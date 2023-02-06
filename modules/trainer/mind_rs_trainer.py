import math
import os
from collections import defaultdict
from pathlib import Path

import torch
import torch.distributed
import pandas as pd
import numpy as np
from torch.utils.data import DataLoader
from tqdm import tqdm

from config.configuration import Configuration
from dataset import ImpressionDataset
from trainer import NCTrainer
from utils import gather_dict, load_batch_data, get_news_embeds


class MindRSTrainer(NCTrainer):
    """
    Trainer class
    """

    def __init__(self, model, config: Configuration, data_loader, **kwargs):
        super().__init__(model, config, data_loader, **kwargs)
        self.valid_interval = config.get("valid_interval", 0.1)
        self.fast_evaluation = config.get("fast_evaluation", True)
        self.topic_variant = config.get("topic_variant", "base")
        self.train_strategy = config.get("train_strategy", "pair_wise")
        self.mind_loader = data_loader
        self.behaviors = data_loader.valid_set.behaviors

    def _validation(self, epoch, batch_idx, do_monitor=True):
        # do validation when reach the interval
        log = {"epoch/step": f"{epoch}/{batch_idx}"}
        log.update(**{"val_" + k: v for k, v in self._valid_epoch(extra_str=f"{epoch}_{batch_idx}").items()})
        if do_monitor:
            self._monitor(log, epoch)
        self.model.train()  # reset to training mode
        self.train_metrics.reset()
        return log

    def _train_epoch(self, epoch):
        """
        Training logic for an epoch
        :param epoch: Integer, current training epoch.
        :return: A log that contains average loss and metric in this epoch.
        """
        self.model.train()
        self.train_metrics.reset()
        length = len(self.train_loader)
        bar = tqdm(enumerate(self.train_loader), total=length)
        # self._validation(epoch, 0)
        for batch_idx, batch_dict in bar:
            # load data to device
            batch_dict = load_batch_data(batch_dict, self.device)
            # setup model and train model
            self.optimizer.zero_grad()
            output = self.model(batch_dict)
            loss = self.criterion(output["pred"], batch_dict["label"])
            bar_description = f"Train Epoch: {epoch} Loss: {loss.item()}"
            if self.entropy_constraint:
                loss += self.alpha * output["entropy"]
                bar_description += f" Entropy loss: {self.alpha * output['entropy'].item()}"
            if self.topic_variant == "variational_topic":
                # loss += self.beta * output["kl_divergence"]
                bar_description += f" KL divergence: {output['kl_divergence'].item()}"
            self.accelerator.backward(loss)
            self.optimizer.step()
            # record loss
            self.writer.set_step((epoch - 1) * self.len_epoch + batch_idx)
            self.train_metrics.update("loss", loss.item())
            if batch_idx % self.log_step == 0:
                bar.set_description(bar_description)
            if batch_idx == self.len_epoch:
                break
            if (batch_idx + 1) % math.ceil(length * self.valid_interval) == 0 and (batch_idx + 1) < length:
                self._validation(epoch, batch_idx)
        if self.lr_scheduler is not None:
            self.lr_scheduler.step()
        return self._validation(epoch, length, False)

    def _valid_epoch(self, model=None, valid_set=None, extra_str=None, prefix="val"):
        """
        Validate after training an epoch
        :return: A log that contains information about validation
        """
        result_dict = {}
        impression_bs = self.config.get("impression_batch_size", 128)
        valid_method = self.config.get("valid_method", "fast_evaluation")
        if torch.distributed.is_initialized():
            model = self.model.module if model is None else model.module
        else:
            model = self.model if model is None else model
        valid_set = self.mind_loader.valid_set if valid_set is None else valid_set
        model.eval()
        weight_dict = defaultdict(lambda: [])
        topic_variant = self.config.get("topic_variant", "base")
        return_weight = self.config.get("return_weight", False)
        entropy_constraint = self.config.get("entropy_constraint", False)
        saved_weight_num = self.config.get("saved_weight_num", 250)
        with torch.no_grad():
            try:  # try to do fast evaluation: cache news embeddings
                if valid_method == "fast_evaluation" and not return_weight and not entropy_constraint and \
                        not topic_variant == "variational_topic":
                    news_loader = self.mind_loader.news_loader
                    news_embeds = get_news_embeds(model, news_loader, device=self.device, accelerator=self.accelerator)
                else:
                    news_embeds = None
            except KeyError or RuntimeError:  # slow evaluation: re-calculate news embeddings every time
                news_embeds = None
            imp_set = ImpressionDataset(valid_set, news_embeds, selected_imp=self.config.get("selected_imp", None))
            valid_loader = DataLoader(imp_set, impression_bs, collate_fn=self.mind_loader.fn)
            valid_loader = self.accelerator.prepare_data_loader(valid_loader)
            for vi, batch_dict in tqdm(enumerate(valid_loader), total=len(valid_loader), desc="Impressions-Validation"):
                batch_dict = load_batch_data(batch_dict, self.device)
                label = batch_dict["label"].cpu().numpy()
                out_dict = model(batch_dict)    # run model
                pred = out_dict["pred"].cpu().numpy()
                can_len = batch_dict["candidate_length"].cpu().numpy()
                his_len = batch_dict["history_length"].cpu().numpy()
                for i in range(len(label)):
                    index = batch_dict["impression_index"][i].cpu().tolist()  # record impression index
                    result_dict[index] = {m.__name__: m(label[i][:can_len[i]], pred[i][:can_len[i]])
                                          for m in self.metric_funcs}
                    if return_weight:
                        saved_items = {
                            "impression_index": index, "results": result_dict[index], "label": label[i][:can_len[i]],
                            "candidate_index": batch_dict["candidate_index"][i][:can_len[i]].cpu().tolist(),
                            "history_index": batch_dict["history_index"][i][:his_len[i]].cpu().numpy(),
                            "pred_score": pred[i][:can_len[i]]
                        }
                        for name, indices in saved_items.items():
                            weight_dict[name].append(indices)
                        for name, weight in out_dict.items():
                            if "weight" in name:
                                if "candidate" in name:
                                    length = can_len[i]
                                else:
                                    length = his_len[i]
                                weight_dict[name].append(weight[i][:length].cpu().numpy())
                if vi >= saved_weight_num and return_weight:
                    break
            result_dict = gather_dict(result_dict)  # gather results
            eval_result = dict(np.round(pd.DataFrame.from_dict(result_dict, orient="index").mean(), 4))  # average
            if self.config.get("evaluate_topic_by_epoch", False) and self.config.get("topic_evaluation_method", None):
                eval_result.update(self.topic_evaluation(model, self.mind_loader.word_dict, extra_str=extra_str))
                self.accelerator.wait_for_everyone()
                # self.logger.info(f"validation time: {time.time() - start}")
        if return_weight and self.accelerator.is_main_process:
            weight_dir = Path(self.config["model_dir"], "weight")
            os.makedirs(weight_dir, exist_ok=True)
            weight_path = weight_dir / f"{self.config.get('head_num')}.pt"
            if weight_path.exists():
                old_weights = torch.load(weight_path)
                for key in weight_dict.keys():
                    weight_dict[key].extend(old_weights[key])
            torch.save(dict(weight_dict), weight_path)
            self.logger.info(f"Saved weight to {weight_path}")
        return eval_result

    def evaluate(self, dataset, model, epoch=0, prefix="val"):
        """call this method after training"""
        model.eval()
        log = self._valid_epoch(model, dataset, prefix=prefix)
        for name, p in model.named_parameters():  # add histogram of model parameters to the tensorboard
            self.writer.add_histogram(name, p, bins="auto")
        return {f"{prefix}_{k}": v for k, v in log.items()}  # return log with prefix
