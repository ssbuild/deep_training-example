# -*- coding: utf-8 -*-
import json
import typing

import numpy as np
import torch
from deep_training.data_helper import DataHelper
from deep_training.data_helper import ModelArguments, TrainingArguments, DataArguments, \
    PrefixModelArguments
from deep_training.data_helper import load_tokenizer_and_config_with_args
from deep_training.nlp.models.prefixtuning import PrefixTransformerPointer
from deep_training.nlp.models.transformer import TransformerMeta
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from torch.utils.data import DataLoader, IterableDataset
from transformers import HfArgumentParser, BertTokenizer

train_info_args = {
    'devices': 1,
    'data_backend': 'memory_raw',
    'model_type':  'bert',
    'model_name_or_path': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name':  '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name':  '/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train':  True,
    'do_eval': True,
    'train_file':  '/data/nlp/nlp_train_data/clue/cluener/train.json',
    'eval_file':  '/data/nlp/nlp_train_data/clue/cluener/dev.json',
    'test_file':  '/data/nlp/nlp_train_data/clue/cluener/test.json',
    'learning_rate':  1e-3,
    'max_epochs':  80,
    'train_batch_size':  140,
    'eval_batch_size':  2,
    'test_batch_size':  2,
    'adam_epsilon':  1e-8,
    'gradient_accumulation_steps':  1,
    'max_grad_norm':  1.0,
    'weight_decay':  0,
    'warmup_steps':  0,
    'output_dir':  './output',
    'max_seq_length':  160,
    'pre_seq_len':  16
}

class NN_DataHelper(DataHelper):
    index = -1
    eval_labels = []
    # 切分成开始
    def on_data_ready(self):
        self.index = -1
    # 切分词
    def on_data_process(self, data: typing.Any, user_data: tuple):
        self.index += 1
        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, label2id, mode = user_data
        sentence, label_dict = data

        tokens = list(sentence) if not do_lower_case else list(sentence.lower())
        input_ids = tokenizer.convert_tokens_to_ids(tokens)
        if len(input_ids) > max_seq_length - 2:
            input_ids = input_ids[:max_seq_length - 2]
        input_ids = [tokenizer.cls_token_id] + input_ids + [tokenizer.sep_token_id]
        attention_mask = [1] * len(input_ids)

        input_ids = np.asarray(input_ids, dtype=np.int32)
        attention_mask = np.asarray(attention_mask, dtype=np.int32)
        seqlen = np.asarray(len(input_ids), dtype=np.int32)
        labels = np.zeros(shape=(len(label2id), max_seq_length, max_seq_length), dtype=np.int32)
        real_label = []

        if label_dict is not None:
            for label_str, o in label_dict.items():
                pts = [_ for a_ in list(o.values()) for _ in a_]
                labelid = label2id[label_str]
                for pt in pts:
                    assert pt[0] <= pt[1]
                    if pt[1] < max_seq_length - 2:
                        labels[labelid, pt[0] + 1, pt[1] + 1] = 1
                    real_label.append((labelid, pt[0], pt[1]))

        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            input_ids = np.pad(input_ids, (0, pad_len), 'constant',
                               constant_values=(tokenizer.pad_token_id, tokenizer.pad_token_id))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))
        d = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'labels': labels,
            'seqlen': seqlen,
        }
        if self.index < 5:
            print(tokens)
            print(input_ids[:seqlen])
            print(attention_mask[:seqlen])
            print(seqlen)

        if mode == 'eval':
            self.eval_labels.append(real_label)
        # if mode == 'eval':
        #     d['real_label'] = np.asarray(bytes(json.dumps(real_label, ensure_ascii=False), encoding='utf-8'))
        return d

    # 读取标签
    def on_get_labels(self, files: typing.List[str]):
        labels = [
            'address', 'book', 'company', 'game', 'government', 'movie', 'name', 'organization', 'position', 'scene'
        ]
        labels = list(set(labels))
        labels = sorted(labels)
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
                    D.append((jd['text'], jd.get('label', None)))
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
        o['labels'] = o['labels'][:, :, :max_len, :max_len]
        return o

class MyTransformer(PrefixTransformerPointer, metaclass=TransformerMeta):
    def __init__(self,eval_labels, *args, **kwargs):
        super(MyTransformer, self).__init__(*args, **kwargs)
        self.model.eval_labels = eval_labels


if __name__== '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments, PrefixModelArguments))
    model_args, training_args, data_args, prompt_args = parser.parse_dict(train_info_args)

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


    train_datasets = dataHelper.load_dataset(train_files,shuffle=True)
    eval_datasets = dataHelper.load_dataset(eval_files)
    test_datasets = dataHelper.load_dataset(test_files)
    if train_datasets:
        train_datasets = DataLoader(train_datasets,batch_size=training_args.train_batch_size,collate_fn=dataHelper.collate_fn,shuffle=False if isinstance(train_datasets, IterableDataset) else True)
    if eval_datasets:
        eval_datasets = DataLoader(eval_datasets,batch_size=training_args.eval_batch_size,collate_fn=dataHelper.collate_fn)
    if test_datasets:
        test_datasets = DataLoader(test_datasets,batch_size=training_args.test_batch_size,collate_fn=dataHelper.collate_fn)

    print('*' * 30,train_datasets,eval_datasets,test_datasets)

    model = MyTransformer(dataHelper.eval_labels,with_efficient=True,prompt_args=prompt_args,config=config,model_args=model_args,training_args=training_args)
    checkpoint_callback = ModelCheckpoint(monitor="val_f1",  every_n_epochs=1)
    trainer = Trainer(
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

    if train_datasets:
        trainer.fit(model, train_dataloaders=train_datasets,val_dataloaders=eval_datasets)

    if eval_datasets:
        trainer.validate(model, dataloaders=eval_datasets)

    if test_datasets:
        trainer.test(model, dataloaders=test_datasets)
