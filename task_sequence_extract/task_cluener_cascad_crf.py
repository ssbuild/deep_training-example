# -*- coding: utf-8 -*-
import json
import os
import sys
import typing

from deep_training.nlp.models.transformer import TransformerMeta
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities.types import EPOCH_OUTPUT
from deep_training.data_helper import DataHelper
import torch
import numpy as np
from pytorch_lightning import Trainer
from deep_training.data_helper import make_dataset_with_args, load_dataset_with_args, \
    load_tokenizer_and_config_with_args
from deep_training.nlp.models.crf_cascad import TransformerForCascadCRF,extract_lse
from transformers import HfArgumentParser, BertTokenizer
from deep_training.data_helper import ModelArguments, DataArguments, TrainingArguments
from deep_training.nlp.metrics.pointer import metric_for_pointer


train_info_args = {
    'devices': 1,
    'data_backend':'memory_raw',
    'model_type':'bert',
    'model_name_or_path':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name':'/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'do_eval': True,
    'train_file':'/data/nlp/nlp_train_data/clue/cluener/train.json',
    'eval_file':'/data/nlp/nlp_train_data/clue/cluener/dev.json',
    'test_file':'/data/nlp/nlp_train_data/clue/cluener/test.json',
    'optimizer': 'adamw',
    'learning_rate':5e-5,
    'learning_rate_for_task':1e-4,
    'max_epochs':15,
    'train_batch_size': 64,
    'eval_batch_size':2,
    'test_batch_size':2,
    'adam_epsilon':1e-8,
    'gradient_accumulation_steps':1,
    'max_grad_norm':1.0,
    'weight_decay':0,
    'warmup_steps': 0,
    'output_dir': './output',
    'train_max_seq_length': 380,
    'eval_max_seq_length': 512,
    'test_max_seq_length': 512,
}



class NN_DataHelper(DataHelper):
    eval_labels = []

    index = 1
    def on_data_ready(self):
        self.index = -1
    # 切分词
    def on_data_process(self, data: typing.Any, user_data: tuple):
        self.index += 1
        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, _, mode = user_data
        sentence, label_dict = data

        tokens = list(sentence) if not do_lower_case else list(sentence.lower())
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        if len(input_ids) > max_seq_length - 2:
            input_ids = input_ids[:max_seq_length - 2]
        input_ids = [tokenizer.cls_token_id] + input_ids + [tokenizer.sep_token_id]
        attention_mask = [1] * len(input_ids)

        input_ids = np.asarray(input_ids, dtype=np.int64)
        attention_mask = np.asarray(attention_mask, dtype=np.int64)
        seqlen = np.asarray(len(input_ids), dtype=np.int64)

        seqs2id = self.task_specific_params['seqs2id']
        ents2id = self.task_specific_params['ents2id']
        seqs_labels = np.zeros(shape=(seqlen,), dtype=np.int64)
        ents_labels = np.zeros(shape=(seqlen,), dtype=np.int64)

        real_label = []
        for label_str, o in label_dict.items():
            pts = [_ for a_ in list(o.values()) for _ in a_]
            for pt in pts:
                real_label.append((ents2id[label_str],pt[0],pt[1]))
                if pt[1] > seqlen - 2:
                    continue
                pt[0] += 1
                pt[1] += 1
                span_len = pt[1] - pt[0] + 1
                if span_len == 1:
                    seqs_labels[pt[0]] = seqs2id['B']
                    ents_labels[pt[0]] = ents2id[label_str]
                elif span_len >= 2:
                    seqs_labels[pt[0]] = seqs2id['B']
                    ents_labels[pt[0]] = ents2id[label_str]
                    for i in range(span_len - 1):
                        seqs_labels[pt[0] + 1 + i] = seqs2id['I']
                        ents_labels[pt[0] + 1 + i] = ents2id[label_str]

        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            pad_val = tokenizer.pad_token_id
            input_ids = np.pad(input_ids, (0, pad_len), 'constant', constant_values=(pad_val, pad_val))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))
            seqs_labels = np.pad(seqs_labels, (0, pad_len), 'constant', constant_values=(0, 0))
            ents_labels = np.pad(ents_labels, (0, pad_len), 'constant', constant_values=(0, 0))

        d = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'seqs_labels': seqs_labels,
            'ents_labels': ents_labels,
            'seqlen': seqlen,
        }
        if self.index < 5:
            print(tokens)
            print(input_ids[:seqlen])
            print(attention_mask[:seqlen])
            print(seqs_labels[:seqlen])
            print(ents_labels[:seqlen])
            print(seqlen)
        if mode == 'eval':
            self.eval_labels.append(real_label)
        return d

    def on_task_specific_params(self):
        labels = ['O' ,'B','I']
        labels_e = [
            'address','book','company','game','government','movie','name','organization','position','scene'
        ]
        task_specific_params = {
            'seqs2id': {l: i for i, l in enumerate(labels)},
            'id2seqs': {i: l for i, l in enumerate(labels)},
            'ents2id': {l: i for i, l in enumerate(labels_e)},
            'id2ents': {i: l for i, l in enumerate(labels_e)},
        }
        self.task_specific_params = task_specific_params
        return task_specific_params
    #读取标签
    def on_get_labels(self, files: typing.List[str]):
        return None,None

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode:str):
        D = []
        for filename in files:
            with open(filename, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in lines:
                    jd = json.loads(line)
                    if not jd:
                        continue
                    D.append((jd['text'], jd.get('label',None)))
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
        o['seqs_labels'] = o['seqs_labels'][:, :max_len]
        o['ents_labels'] = o['ents_labels'][:,:max_len]
        return o

class MyTransformer(TransformerForCascadCRF, metaclass=TransformerMeta):
    def __init__(self, eval_labels,*args,**kwargs):
        super(MyTransformer, self).__init__(*args,**kwargs)
        self.eval_labels = eval_labels

    def validation_epoch_end(self, outputs: typing.Union[EPOCH_OUTPUT, typing.List[EPOCH_OUTPUT]]) -> None:
        preds,trues = [],[]

        task_specific_params = self.config.task_specific_params
        id2seqs = task_specific_params['id2seqs']
        ents2id = task_specific_params['ents2id']

        eval_labels = self.eval_labels
        for i,o in enumerate(outputs):
            crf_tags,ents_logits,_,_ = o['outputs']
            preds.extend(extract_lse([crf_tags,ents_logits],id2seqs))
            bs = len(crf_tags)
            trues.extend(eval_labels[i * bs: (i + 1) * bs])

        print(preds[:3])
        print(trues[:3])
        f1, str_report = metric_for_pointer(trues, preds, ents2id)
        print(f1)
        print(str_report)
        self.log('val_f1', f1, prog_bar=True)




if __name__ == '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

    dataHelper = NN_DataHelper(data_args.data_backend)
    tokenizer, config, label2id, id2label = load_tokenizer_and_config_with_args(dataHelper, model_args, training_args,data_args)

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
                make_dataset_with_args(dataHelper, data_args.train_file, token_fn_args_dict['train'], data_args,
                                       intermediate_name=intermediate_name, shuffle=True, mode='train'))
        if data_args.do_eval:
            eval_files.append(
                make_dataset_with_args(dataHelper, data_args.eval_file, token_fn_args_dict['eval'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='eval'))
        if data_args.do_test:
            test_files.append(
                make_dataset_with_args(dataHelper, data_args.test_file, token_fn_args_dict['test'], data_args,
                                       intermediate_name=intermediate_name, shuffle=False, mode='test'))

    dm = load_dataset_with_args(dataHelper, training_args, train_files, eval_files, test_files)
    model = MyTransformer(dataHelper.eval_labels,config=config,model_args=model_args,training_args=training_args)
    checkpoint_callback = ModelCheckpoint(monitor='val_f1',save_top_k=1,every_n_epochs=1)
    trainer = Trainer(
        log_every_n_steps = 10,
        callbacks=[checkpoint_callback],
         max_epochs=training_args.max_epochs,
        max_steps=training_args.max_steps,
        accelerator="gpu",
        devices=data_args.devices,  
        enable_progress_bar=True,
        default_root_dir=data_args.output_dir,
        gradient_clip_val=training_args.max_grad_norm,
        accumulate_grad_batches = training_args.gradient_accumulation_steps,
        num_sanity_val_steps=0,
    )


    if data_args.do_train:
        trainer.fit(model, datamodule=dm)

    if data_args.do_eval:
        trainer.validate(model, datamodule=dm)

    if data_args.do_test:
        trainer.test(model, datamodule=dm)
