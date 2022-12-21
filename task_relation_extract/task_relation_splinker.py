# -*- coding: utf-8 -*-
import copy
import json
import os
import sys
from torch.utils.data import DataLoader, IterableDataset
from deep_training.nlp.models.transformer import TransformerMeta
from pytorch_lightning.utilities.types import EPOCH_OUTPUT

from deep_training.nlp.models.splinker.splinker import extract_spoes
import typing
import numpy as np
from pytorch_lightning.callbacks import ModelCheckpoint
from deep_training.data_helper import DataHelper
import torch
from pytorch_lightning import Trainer
from deep_training.data_helper import load_tokenizer_and_config_with_args
from transformers import HfArgumentParser, BertTokenizer
from deep_training.data_helper import ModelArguments, TrainingArguments, DataArguments
from deep_training.nlp.models.splinker import TransformerForSplinker
from seqmetric.metrics.spo_labeling import spo_report,get_report_from_string

train_info_args = {
    'devices': 1,
    'data_backend': 'memory_raw',
    'model_type': 'bert',
    'model_name_or_path': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'do_eval': True,
    # 'train_file': '/data/nlp/nlp_train_data/relation/law/step1_train-fastlabel.json',
    # 'eval_file': '/data/nlp/nlp_train_data/relation/law/step1_train-fastlabel.json',
    # 'label_file': '/data/nlp/nlp_train_data/relation/law/relation_label.json',
    'train_file': '/data/nlp/nlp_train_data/myrelation/re_labels.json',
    'eval_file': '/data/nlp/nlp_train_data/myrelation/re_labels.json',
    'label_file': '/data/nlp/nlp_train_data/myrelation/labels.json',
    'learning_rate': 5e-5,
    'max_epochs': 10,
    'train_batch_size': 16,
    'eval_batch_size': 8,
    'test_batch_size': 2,
    'adam_epsilon': 1e-8,
    'gradient_accumulation_steps': 1,
    'max_grad_norm': 1.0,
    'weight_decay': 0,
    'warmup_steps': 0,
    'output_dir': './output',
    'max_seq_length': 320,
}


class NN_DataHelper(DataHelper):
    index = 0
    def on_data_ready(self):
        self.index = -1
    # 切分词
    def on_data_process(self, data: typing.Any, user_data: tuple):
        self.index += 1

        tokenizer: BertTokenizer
        tokenizer, max_seq_length, predicate2id, mode = user_data
        sentence, entities, re_list = data
        spo_list = re_list

        tokens = list(sentence)
        if len(tokens) > max_seq_length - 2:
            tokens = tokens[0:(max_seq_length - 2)]
        input_ids = tokenizer.convert_tokens_to_ids(['[CLS]'] + tokens + ['[SEP]'])
        seqlen = len(input_ids)
        attention_mask = [1] * seqlen
        input_ids = np.asarray(input_ids, dtype=np.int32)
        attention_mask = np.asarray(attention_mask, dtype=np.int32)
        num_labels = len(predicate2id)
        if spo_list is not None:
            labels = np.zeros(shape=(seqlen - 2, num_labels * 2 + 2), dtype=np.int32)
            for s, p, o in spo_list:
                if s[1] >= seqlen - 2 or o[1] >= seqlen - 2:
                    continue
                s_ids = [s[0], s[1]]
                o_ids = [o[0], o[1]]
                label_for_s = predicate2id[p] + 2
                label_for_o = label_for_s + num_labels
                slen = s_ids[1] - s_ids[0] + 1
                labels[s[0]][label_for_s] = 1
                for i in range(slen - 1):
                    labels[s[0] + i + 1][1] = 1
                labels[o[0]][label_for_o] = 1
                olen = o_ids[1] - o_ids[0] + 1
                for i in range(olen - 1):
                    labels[o[0] + i + 1][1] = 1
            for i in range(seqlen - 2):
                if not np.any(labels[i]):
                    labels[i][0] = 1
            edge = np.expand_dims(np.asarray([1] + [0] * (num_labels * 2 + 1), dtype=np.int32), axis=0)
            labels = np.concatenate([edge, labels, edge], axis=0)
        else:
            labels = np.zeros(shape=(seqlen, num_labels * 2 + 2), dtype=np.int32)

        pad_len = max_seq_length - len(input_ids)
        if pad_len:
            pad_val = tokenizer.pad_token_id
            input_ids = np.pad(input_ids, (0, pad_len), 'constant', constant_values=(pad_val, pad_val))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))
            labels = np.concatenate([labels, np.zeros(shape=(pad_len, num_labels * 2 + 2))], axis=0)

        mask = np.logical_and(input_ids != tokenizer.pad_token_id,
                              np.logical_and(input_ids != tokenizer.cls_token_id, input_ids != tokenizer.sep_token_id))

        d = {
            "input_ids": np.array(input_ids, dtype=np.int32),
            "attention_mask": np.asarray(attention_mask, dtype=np.int32),
            "mask": np.asarray(mask, dtype=np.int32),
            "labels": np.array(labels, dtype=np.float32),
            "seqlen": np.array(seqlen, dtype=np.int32),
        }
        return d

    # 读取标签
    def on_get_labels(self, files: typing.List):
        labels = []
        label_filename = files[0]
        with open(label_filename, mode='r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                jd = json.loads(line)
                if not jd:
                    continue
                larr = [jd['subject'], jd['predicate'], jd['object']]
                labels.append('+'.join(larr))
        label2id = {label: i for i, label in enumerate(labels)}
        id2label = {i: label for i, label in enumerate(labels)}
        return label2id, id2label

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode: str):
        D = []
        for filename in files:
            with open(filename, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    jd = json.loads(line)
                    if not jd:
                        continue

                    entities = jd.get('entities', None)
                    re_list = jd.get('re_list', None)

                    if entities:
                        entities_label = []
                        for k, v in entities.items():
                            pts = list(v.values())[0]
                            for pt in pts:
                                entities_label.append((k, pt[0], pt[1]))
                    else:
                        entities_label = None

                    if re_list is not None:
                        re_list_label = []
                        for re_node in re_list:
                            for l, relation in re_node.items():
                                s = relation[0]
                                o = relation[1]
                                re_list_label.append((
                                    # (s['pos'][0], s['pos'][1],s['label']),
                                    # l,
                                    # (o['pos'][0], o['pos'][1],o['label'])
                                    (s['pos'][0], s['pos'][1]),
                                    '+'.join([s['label'], l, o['label']]),
                                    (o['pos'][0], o['pos'][1])
                                ))
                    else:
                        re_list_label = None

                    D.append((jd['text'], entities_label, re_list_label))
        return D

    @staticmethod
    def collate_fn(batch):
        o = {}
        for i, b in enumerate(batch):
            if i == 0:
                for k in b:
                    o[k] = [torch.tensor(b[k])]
            else:
                for k in b:
                    o[k].append(torch.tensor(b[k]))
        for k in o:
            o[k] = torch.stack(o[k])

        max_len = torch.max(o.pop('seqlen'))

        o['input_ids'] = o['input_ids'][:, :max_len]
        o['attention_mask'] = o['attention_mask'][:, :max_len]
        if 'token_type_ids' in o:
            o['token_type_ids'] = o['token_type_ids'][:, :max_len]

        o['mask'] = o['mask'][:, :max_len]
        o['labels'] = o['labels'][:, :max_len]
        return o

class MyTransformer(TransformerForSplinker, metaclass=TransformerMeta):
    def __init__(self, *args, **kwargs):
        super(MyTransformer, self).__init__(*args, **kwargs)
        self.index = 0

    def validation_epoch_end(self, outputs: typing.Union[EPOCH_OUTPUT, typing.List[EPOCH_OUTPUT]]) -> None:
        self.index += 1
        if self.index < 1:
            self.log('val_f1', 0.0)
            return


        y_preds, y_trues = [], []
        for o in outputs:
            logits, seqlen, labels = o['outputs']
            pred = extract_spoes(logits, seqlen, self.config.id2label)
            true = extract_spoes(labels, seqlen, self.config.id2label)
            y_preds.extend(pred)
            y_trues.extend(true)


        str_report = spo_report(y_trues,y_preds,self.config.label2id,col_space=10)
        report = get_report_from_string(str_report,metric='macro')
        f1 = report[-2]
        print(str_report)
        print(f1)
        self.log('val_f1', f1, prog_bar=True)

def get_trainer():
    checkpoint_callback = ModelCheckpoint(monitor="val_f1", every_n_epochs=1)
    trainer = Trainer(
        callbacks=[checkpoint_callback],
        max_epochs=training_args.max_epochs,
        max_steps=training_args.max_steps,
        accelerator="gpu",
        devices=data_args.devices,
        enable_progress_bar=True,
        default_root_dir=data_args.output_dir,
        gradient_clip_val=training_args.max_grad_norm,
        accumulate_grad_batches=training_args.gradient_accumulation_steps,
        num_sanity_val_steps=0,
    )
    return trainer

if __name__ == '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

    trainer = get_trainer()
    dataHelper = NN_DataHelper(data_args.data_backend)
    tokenizer, config, label2id, id2label = load_tokenizer_and_config_with_args(dataHelper, model_args, training_args,
                                                                                data_args)
    token_fn_args_dict = {
        'train': (tokenizer, data_args.train_max_seq_length, model_args.do_lower_case, label2id, 'train'),
        'eval': (tokenizer, data_args.eval_max_seq_length, model_args.do_lower_case, label2id, 'eval'),
        'test': (tokenizer, data_args.test_max_seq_length, model_args.do_lower_case, label2id, 'test')
    }

    N = 1
    train_files, eval_files, test_files = [], [], []
    for i in range(N):
        intermediate_name = data_args.intermediate_name + '_{}'.format(i)
        if data_args.do_train:
            train_files.append(
                dataHelper.make_dataset_with_args(data_args.train_file, token_fn_args_dict['train'], data_args,
                                       intermediate_name=intermediate_name, shuffle=True, mode='train'))
        if data_args.do_eval:
            eval_files.append(
                dataHelper.make_dataset_with_args(data_args.eval_file, token_fn_args_dict['eval'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='eval'))
        if data_args.do_test:
            test_files.append(
                dataHelper.make_dataset_with_args(data_args.test_file, token_fn_args_dict['test'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='test'))

    train_datasets = dataHelper.load_dataset(train_files,shuffle=True,num_processes=trainer.world_size,process_index=trainer.global_rank,infinite=True)
    eval_datasets = dataHelper.load_dataset(eval_files,num_processes=trainer.world_size,process_index=trainer.global_rank)
    test_datasets = dataHelper.load_dataset(test_files,num_processes=trainer.world_size,process_index=trainer.global_rank)
    if train_datasets is not None:
        train_datasets = DataLoader(train_datasets,batch_size=training_args.train_batch_size,collate_fn=dataHelper.collate_fn,shuffle=False if isinstance(train_datasets, IterableDataset) else True)
    if eval_datasets is not None:
        eval_datasets = DataLoader(eval_datasets,batch_size=training_args.eval_batch_size,collate_fn=dataHelper.collate_fn)
    if test_datasets is not None:
        test_datasets = DataLoader(test_datasets,batch_size=training_args.test_batch_size,collate_fn=dataHelper.collate_fn)

    

    model = MyTransformer(config=config, model_args=model_args, training_args=training_args)
    if train_datasets is not None:
        trainer.fit(model, train_dataloaders=train_datasets,val_dataloaders=eval_datasets)

    if eval_datasets is not None:
        trainer.validate(model, dataloaders=eval_datasets)

    if test_datasets is not None:
        trainer.test(model, dataloaders=test_datasets)
