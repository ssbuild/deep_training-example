# -*- coding: utf-8 -*-
import json
import typing

import numpy as np
import torch
from deep_training.data_helper import DataHelper
from deep_training.data_helper import ModelArguments, TrainingArguments, DataArguments
from deep_training.data_helper import load_tokenizer_and_config_with_args
from deep_training.nlp.models.transformer import TransformerForSequenceClassification, TransformerMeta
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities.types import EPOCH_OUTPUT
from sklearn.metrics import f1_score, classification_report
from torch.utils.data import DataLoader, IterableDataset
from transformers import HfArgumentParser, BertTokenizer

train_info_args = {
    'devices':  '1',
    'data_backend': 'memory_raw',
    'model_type': 'bert',
    'model_name_or_path': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'do_eval': True,
    'train_file': '/data/nlp/nlp_train_data/clue/tnews/train.json',
    'eval_file': '/data/nlp/nlp_train_data/clue/tnews/dev.json',
    'test_file': '/data/nlp/nlp_train_data/clue/tnews/test.json',
    'label_file': '/data/nlp/nlp_train_data/clue/tnews/labels.json',
    'learning_rate': 5e-5,
    'max_epochs': 3,
    'train_batch_size': 10,
    'test_batch_size': 2,
    'adam_epsilon': 1e-8,
    'gradient_accumulation_steps': 1,
    'max_grad_norm': 1.0,
    'weight_decay': 0,
    'warmup_steps': 0,
    'output_dir': './output',
    'train_max_seq_length': 380,
    'eval_max_seq_length': 512,
    'test_max_seq_length': 512,
}


class NN_DataHelper(DataHelper):
    # 切分词
    def on_data_process(self,data: typing.Any, user_data: tuple):
        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, label2id, mode = user_data
        sentence,label_str = data

        o = tokenizer(sentence, max_length=max_seq_length, truncation=True, add_special_tokens=True, )
        input_ids = np.asarray(o['input_ids'], dtype=np.int64)
        attention_mask = np.asarray(o['attention_mask'], dtype=np.int64)

        labels = np.asarray(label2id[label_str] if label_str is not None else 0,dtype=np.int64)
        seqlen = np.asarray(len(input_ids), dtype=np.int64)
        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            pad_val = tokenizer.pad_token_id
            input_ids = np.pad(input_ids, (0, pad_len), 'constant', constant_values=(pad_val, pad_val))
            attention_mask = np.pad(attention_mask, (0, pad_len), 'constant', constant_values=(0, 0))
        d = {
            'input_ids': input_ids,
            'attention_mask': attention_mask,
            'labels': labels,
            'seqlen': seqlen
        }
        return d

    # 读取标签
    def on_get_labels(self, files: typing.List[str]):
        if files is None:
            return None, None
        label_fname = files[0]
        is_json_file = label_fname.endswith('.json')
        D = set()
        with open(label_fname, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            for line in lines:
                line = line.replace('\r\n', '').replace('\n', '')
                if not line: continue
                if is_json_file:
                    jd = json.loads(line)
                    line = jd['label']
                D.add(line)
        label2id = {label: i for i, label in enumerate(D)}
        id2label = {i: label for i, label in enumerate(D)}
        return label2id, id2label

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
                    D.append((jd['sentence'], jd.get('label',None)))
        return D[0:1000] if mode == 'train' else D[:100]


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
        return o

class MyTransformer(TransformerForSequenceClassification, metaclass=TransformerMeta):
    def __init__(self,*args,**kwargs):
        super(MyTransformer, self).__init__(*args,**kwargs)

    def compute_loss(self,batch,batch_idx) -> tuple:
        outputs = self(**batch)
        labels = batch.get('labels',None)
        if labels is not None:
            loss, logits = outputs[0:2]
            acc = torch.sum(torch.eq(labels.view(-1),
                                     torch.argmax(logits, dim=1, keepdim=False))) / labels.view(-1).size()[0]
            loss_dict = {
                'loss': loss,
                'acc': acc
            }
            outputs = (loss_dict, logits, labels)
        else:
            outputs = (outputs[0],)

        return outputs

    def validation_epoch_end(self, outputs: typing.Union[EPOCH_OUTPUT, typing.List[EPOCH_OUTPUT]]) -> None:
        preds_all, labels_all = [], []
        for output in outputs:
            preds, labels = output['outputs']
            preds = np.argmax(preds,-1)
            for p, l in zip(preds, labels):
                preds_all.append(p)
                labels_all.append(int(l))

        preds_all = np.asarray(preds_all,dtype=np.int32)
        labels_all = np.asarray(labels_all, dtype=np.int32)
        f1 = f1_score(labels_all, preds_all, average='micro')
        report = classification_report(labels_all, preds_all, digits=4,
                                       labels=list(self.config.label2id.values()),target_names=list(self.config.label2id.keys()))

        print(f1, report)
        self.log('val_f1', f1)


def get_trainer():
    checkpoint_callback = ModelCheckpoint(monitor="val_f1", save_last=False, every_n_epochs=1)
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

if __name__== '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

    trainer = get_trainer()
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

    train_datasets = dataHelper.load_dataset(train_files,shuffle=True,num_processes=trainer.world_size,process_index=trainer.global_rank)
    eval_datasets = dataHelper.load_dataset(eval_files,num_processes=trainer.world_size,process_index=trainer.global_rank)
    test_datasets = dataHelper.load_dataset(test_files,num_processes=trainer.world_size,process_index=trainer.global_rank)
    if train_datasets is not None:
        train_datasets = DataLoader(train_datasets, batch_size=training_args.train_batch_size,
                                    collate_fn=dataHelper.collate_fn,
                                    shuffle=False if isinstance(train_datasets, IterableDataset) else True)
    if eval_datasets is not None:
        eval_datasets = DataLoader(eval_datasets, batch_size=training_args.eval_batch_size,
                                   collate_fn=dataHelper.collate_fn)
    if test_datasets is not None:
        test_datasets = DataLoader(test_datasets, batch_size=training_args.test_batch_size,
                                   collate_fn=dataHelper.collate_fn)
    

    model = MyTransformer(config=config,model_args=model_args,training_args=training_args)
   

    if train_datasets is not None:
        trainer.fit(model, train_dataloaders=train_datasets,val_dataloaders=eval_datasets)

    if eval_datasets is not None:
        trainer.validate(model, dataloaders=eval_datasets)

    if test_datasets is not None:
        trainer.test(model, dataloaders=test_datasets)
