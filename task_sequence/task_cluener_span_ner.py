# -*- coding: utf-8 -*-
import json
import os
import sys
import typing
from functools import partial


from deep_training.nlp.models.transformer import TransformerMeta
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.utilities.types import EPOCH_OUTPUT
from deep_training.data_helper import DataHelper
import torch
import numpy as np
from pytorch_lightning import Trainer
from deep_training.data_helper import make_dataset_with_args, load_dataset_with_args, \
    load_tokenizer_and_config_with_args
from deep_training.nlp.models.span_ner import TransformerForSpanNer,extract_lse_singlelabel,extract_lse_mutilabel
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
    'learning_rate':5e-5,
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


#实体是否有多标签
with_mutilabel = False


class NN_DataHelper(DataHelper):
    def __init__(self,with_mutilabel, backend,*args, **kwargs):
        super(NN_DataHelper, self).__init__(backend,*args, **kwargs)
        self.with_mutilabel = with_mutilabel

    eval_labels = []


    index = 1
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

        input_ids = np.asarray(input_ids, dtype=np.int64)
        attention_mask = np.asarray(attention_mask, dtype=np.int64)
        seqlen = np.asarray(len(input_ids), dtype=np.int64)

        if self.with_mutilabel:
            labels = np.zeros(shape=(max_seq_length,len(label2id),2), dtype=np.int64)
        else:
            labels = np.zeros(shape=(max_seq_length, 2), dtype=np.int64)
        real_label = []
        for label_str, o in label_dict.items():
            pts = [_ for a_ in list(o.values()) for _ in a_]
            for pt in pts:
                assert pt[0] <= pt[1]
                l = label2id[label_str]
                real_label.append((l,pt[0],pt[1]))
                if pt[1] > seqlen - 2:
                    continue
                pt[0] += 1
                pt[1] += 1
                if self.with_mutilabel:
                    labels[pt[0],l, 0] = 1
                    labels[pt[1],l, 1] = 1
                else:
                    labels[pt[0], 0] = l + 1
                    labels[pt[1], 1] = l + 1


        pad_len = max_seq_length - len(input_ids)
        if pad_len > 0:
            pad_val = tokenizer.pad_token_id
            input_ids = np.pad(input_ids, (0, pad_len), 'constant', constant_values=(pad_val, pad_val))
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
            # print(labels[:seqlen])
            print(seqlen)
        if mode == 'eval':
            self.eval_labels.append(real_label)
        return d

    #读取标签
    def on_get_labels(self, files: typing.List[str]):
        labels = [
            'address','book','company','game','government','movie','name','organization','position','scene'
        ]

        labels = list(set(labels))
        label2id = {label: i for i, label in enumerate(labels)}
        id2label = {i: label for i, label in enumerate(labels)}
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
        o['labels'] = o['labels'][:,:max_len]
        return o

class MyTransformer(TransformerForSpanNer, metaclass=TransformerMeta):
    def __init__(self,eval_labels, *args,**kwargs):
        super(MyTransformer, self).__init__(*args,**kwargs)
        self.with_mutilabel = self.model.with_mutilabel
        self.eval_labels = eval_labels

    def validation_epoch_end(self, outputs: typing.Union[EPOCH_OUTPUT, typing.List[EPOCH_OUTPUT]]) -> None:
        label2id = self.config.label2id
        threshold = 0.5
        top_n = 1 # 实体最大交叉包含， 1 不重叠
        preds, trues = [], []
        eval_labels = self.eval_labels

        extract_lse = partial(extract_lse_mutilabel,threshold=threshold,top_n=top_n) if self.with_mutilabel else partial(extract_lse_singlelabel,top_n=top_n)

        if self.with_mutilabel:
            for i, o in enumerate(outputs):
                logits, _ = o['outputs']
                preds.extend(extract_lse(logits))
                bs = len(logits)
                trues.extend(eval_labels[i * bs: (i + 1) * bs])
        else:
            for i, o in enumerate(outputs):
                head_logits,tail_logits, _ = o['outputs']
                preds.extend(extract_lse((head_logits,tail_logits)))
                bs = len(head_logits)
                trues.extend(eval_labels[i * bs: (i + 1) * bs])

        print(preds[:3])
        print(trues[:3])
        f1, str_report = metric_for_pointer(trues, preds, label2id)
        print(f1)
        print(str_report)
        self.log('val_f1', f1, prog_bar=True)

if __name__ == '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments))
    model_args, training_args, data_args = parser.parse_dict(train_info_args)

    #with_mutilabel 是否多标签
    dataHelper = NN_DataHelper(with_mutilabel,data_args.data_backend)
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
    model = MyTransformer(dataHelper.eval_labels,with_mutilabel=with_mutilabel,config=config,model_args=model_args,training_args=training_args)
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
