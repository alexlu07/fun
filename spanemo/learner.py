from fastprogress.fastprogress import format_time, master_bar, progress_bar
from transformers import AdamW, get_linear_schedule_with_warmup
from sklearn.metrics import f1_score, jaccard_score, mean_squared_error
import torch.nn.functional as F
import numpy as np
import pandas as pd
import torch
import time


class EarlyStopping:
    """Early stops the training if validation loss doesn't improve after a given patience.
    Taken from https://github.com/Bjarten/early-stopping-pytorch"""

    def __init__(self, filename, patience=7, verbose=True, delta=0):
        """
        Args:
            patience (int): How long to wait after last time validation loss improved.
                            Default: 7
            verbose (bool): If True, prints a message for each validation loss improvement.
                            Default: False
            delta (float): Minimum change in the monitored quantity to qualify as an improvement.
                            Default: 0
        """
        self.patience = patience
        self.verbose = verbose
        self.counter = 0
        self.best_score = None
        self.early_stop = False
        self.val_loss_min = np.Inf
        self.delta = delta
        self.cur_date = filename

    def __call__(self, val_loss, model):
        score = -val_loss
        if self.best_score is None:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
        elif score < self.best_score + self.delta:
            self.counter += 1
            print(f'EarlyStopping counter: {self.counter} out of {self.patience}')
            if self.counter >= self.patience:
                self.early_stop = True
        else:
            self.best_score = score
            self.save_checkpoint(val_loss, model)
            self.counter = 0

    def save_checkpoint(self, val_loss, model):
        """Saves model when validation loss decrease."""
        if self.verbose:
            print(f'Validation loss decreased ({self.val_loss_min:.6f} --> {val_loss:.6f}).  Saving model ...')
        torch.save(model.state_dict(), 'models/' + self.cur_date + '_checkpoint.pt')
        self.val_loss_min = val_loss


class Trainer(object):
    """
    Class to encapsulate training and validation steps for a pipeline. Based off the "Tonks Library"
    :param model: PyTorch model to use with the Learner
    :param train_data_loader: dataloader for all of the training data
    :param val_data_loader: dataloader for all of the validation data
    :param filename: the best model will be saved using this given name (str)
    """

    def __init__(self, model, train_data_loader, val_data_loader, filename):
        self.model = model
        self.train_data_loader = train_data_loader
        self.val_data_loader = val_data_loader
        self.filename = filename
        self.early_stop = EarlyStopping(self.filename, patience=5)

        assert train_data_loader.dataset.label_type == val_data_loader.dataset.label_type
        self.label_type = val_data_loader.dataset.label_type

    def fit(self, num_epochs, args, device='cuda:0'):
        """
        Fit the PyTorch model
        :param num_epochs: number of epochs to train (int)
        :param args:
        :param device: str (defaults to 'cuda:0')
        """
        num_samples = 0
        samples_seen = []
        loss_log = []

        optimizer, scheduler, step_scheduler_on_batch = self.optimizer(args)
        self.model = self.model.to(device)
        pbar = master_bar(range(num_epochs))
        if self.label_type == "binary":
            headers = ['Train_Loss', 'Val_Loss', 'F1-Macro', 'F1-Micro', 'JS', 'Time']
        else:
            headers = ['Train_Loss', 'Val_Loss', 'RMSE-Macro', 'RMSE-Micro', 'MSE-Micro', 'Time']
        pbar.write(headers, table=True)
        for epoch in pbar:
            epoch += 1
            start_time = time.time()
            self.model.train()
            overall_training_loss = 0.0
            for step, batch in enumerate(progress_bar(self.train_data_loader, parent=pbar)):
                loss, num_rows, _, _, _ = self.model(batch, device)
                overall_training_loss += loss.item() * num_rows

                num_samples += num_rows
                samples_seen.append(num_samples)
                loss_log.append(loss.item())

                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
                optimizer.step()
                if step_scheduler_on_batch:
                    scheduler.step()
                optimizer.zero_grad()

            if not step_scheduler_on_batch:
                scheduler.step()

            overall_training_loss = overall_training_loss / len(self.train_data_loader.dataset)
            overall_val_loss, preds_dict = self.predict(device, pbar)
            y_true, y_pred = preds_dict['y_true'], preds_dict['y_pred']

            str_stats = []
            if self.label_type == "binary":
                stats = [overall_training_loss,
                         overall_val_loss,
                         f1_score(y_true, y_pred, average="macro"),
                         f1_score(y_true, y_pred, average="micro"),
                         jaccard_score(y_true, y_pred, average="samples", zero_division=1)]
            else:
                y_prob = 1/(1+np.exp(-preds_dict['logits']))
                stats = [overall_training_loss,
                         overall_val_loss,
                         mean_squared_error(y_true.flatten(), y_prob.flatten(), squared=False),
                         mean_squared_error(y_true, y_prob, squared=False),
                         mean_squared_error(y_true, y_prob, squared=True)]

            for stat in stats:
                str_stats.append(
                    'NA' if stat is None else str(stat) if isinstance(stat, int) else f'{stat:.4f}'
                )
            str_stats.append(format_time(time.time() - start_time))
            print('epoch#: ', epoch)
            pbar.write(str_stats, table=True)
            torch.save(self.model.state_dict(), f"models/{epoch}.pt")
            self.early_stop(overall_val_loss, self.model)
            if self.early_stop.early_stop:
                print("Early stopping")
                break

        curve_df = pd.DataFrame({"samples": samples_seen, "loss": loss_log})
        curve_df.to_csv("training_curve.csv", index=None)
                
    def optimizer(self, args):
        """

        :param args: object
        """
        optimizer = AdamW([
            {'params': self.model.bert.parameters()},
            {'params': self.model.ffn.parameters(),
             'lr': float(args['ffn_lr'])},
        ], lr=float(args['bert_lr']), correct_bias=True)
        num_train_steps = (int(len(self.train_data_loader.dataset)) /
                           args['train_batch_size']) * args['max_epoch']
        num_warmup_steps = int(num_train_steps * 0.1)

        '''
        we could potentially change the scheduling system 
        '''
        scheduler = get_linear_schedule_with_warmup(optimizer,
                                                    num_warmup_steps=num_warmup_steps,
                                                    num_training_steps=num_train_steps)
        step_scheduler_on_batch = True
        return optimizer, scheduler, step_scheduler_on_batch

    def predict(self, device='cuda:0', pbar=None):
        """
        Evaluate the model on a validation set
        :param device: str (defaults to 'cuda:0')
        :param pbar: fast_progress progress bar (defaults to None)
        :returns: overall_val_loss (float), accuracies (dict{'acc': value}, preds (dict)
        """
        current_size = len(self.val_data_loader.dataset)
        preds_dict = {
            'y_true': np.zeros([current_size, len(self.val_data_loader.dataset[0][3])]),
            'y_pred': np.zeros([current_size, len(self.val_data_loader.dataset[0][3])]),
            'logits': np.zeros([current_size, len(self.val_data_loader.dataset[0][3])]),
        }
        overall_val_loss = 0.0
        self.model.eval()
        with torch.no_grad():
            index_dict = 0
            for step, batch in enumerate(progress_bar(self.val_data_loader, parent=pbar, leave=(pbar is not None))):
                loss, num_rows, y_pred, logits, targets = self.model(batch, device)
                overall_val_loss += loss.item() * num_rows

                current_index = index_dict
                preds_dict['y_true'][current_index: current_index + num_rows, :] = targets
                preds_dict['y_pred'][current_index: current_index + num_rows, :] = y_pred
                preds_dict['logits'][current_index: current_index + num_rows, :] = logits.cpu().numpy()
                index_dict += num_rows

        overall_val_loss = overall_val_loss / len(self.val_data_loader.dataset)
        return overall_val_loss, preds_dict
    

class EvaluateOnTest(object):
    """
    Class to encapsulate evaluation on the test set. Based off the "Tonks Library"
    :param model: PyTorch model to use with the Learner
    :param test_data_loader: dataloader for all of the validation data
    :param model_path: path of the trained model
    """
    def __init__(self, model, test_data_loader, model_path):
        self.model = model
        self.test_data_loader = test_data_loader
        self.model_path = model_path
        self.label_type = test_data_loader.dataset.label_type


    def predict(self, device='cuda:0', pbar=None):
        """
        Evaluate the model on a validation set
        :param device: str (defaults to 'cuda:0')
        :param pbar: fast_progress progress bar (defaults to None)
        :returns: None
        """
        self.model.to(device).load_state_dict(torch.load(self.model_path))
        self.model.eval()
        current_size = len(self.test_data_loader.dataset)
        preds_dict = {
            'y_true': np.zeros([current_size, len(self.test_data_loader.dataset[0][3])]),
            'y_pred': np.zeros([current_size, len(self.test_data_loader.dataset[0][3])]),
            'logits': np.zeros([current_size, len(self.test_data_loader.dataset[0][3])]),
        }
        start_time = time.time()
        with torch.no_grad():
            index_dict = 0
            for step, batch in enumerate(progress_bar(self.test_data_loader, parent=pbar, leave=(pbar is not None))):
                _, num_rows, y_pred, logits, targets = self.model(batch, device)
                current_index = index_dict
                preds_dict['y_true'][current_index: current_index + num_rows, :] = targets
                preds_dict['y_pred'][current_index: current_index + num_rows, :] = y_pred
                preds_dict['logits'][current_index: current_index + num_rows, :] = logits.cpu().numpy()
                index_dict += num_rows

        y_true, y_pred = preds_dict['y_true'], preds_dict['y_pred']

        str_stats = []
        if self.label_type == "binary":
            stats = [f1_score(y_true, y_pred, average="macro"),
                     f1_score(y_true, y_pred, average="micro"),
                     jaccard_score(y_true, y_pred, average="samples", zero_division=1)]
            headers = ['F1-Macro', 'F1-Micro', 'JS', 'Time']
        else:
            y_prob = 1/(1+np.exp(-preds_dict['logits']))
            stats = [mean_squared_error(y_true.flatten(), y_prob.flatten(), squared=False),
                     mean_squared_error(y_true, y_prob, squared=False),
                     mean_squared_error(y_true, y_prob, squared=True)]
            headers = ['RMSE-Macro', 'RMSE-Micro', 'MSE-Micro', 'Time']

        for stat in stats:
            str_stats.append(
                'NA' if stat is None else str(stat) if isinstance(stat, int) else f'{stat:.4f}'
            )
        str_stats.append(format_time(time.time() - start_time))
        print(' '.join('{}: {}'.format(*k) for k in zip(headers, str_stats)))

        return preds_dict