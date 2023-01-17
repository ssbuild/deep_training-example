# -*- coding: utf-8 -*-
# 1、原论文batch_size=512，这里是batch_size=64（实在跑不起这么壕的batch_size）；
# 2、原论文的学习率是5e-5，这里是1e-5；
# 3、原论文的最优dropout比例是0.1，这里是0.3；
# 4、原论文的无监督SimCSE是在额外数据上训练的，这里直接随机选了1万条任务数据训练；
# 5、原文无监督训练的时候还带了个MLM任务，这里只有SimCSE训练。

import copy
import json
import logging
import random
import typing

import numpy as np
import torch
from deep_training.data_helper import DataHelper
from deep_training.data_helper import ModelArguments, TrainingArguments, DataArguments
from deep_training.data_helper import load_tokenizer_and_config_with_args
from deep_training.nlp.models.promptbert_cse import TransformerForPromptbertcse,PromptBertcseArguments
from deep_training.utils.trainer import SimpleModelCheckpoint
from fastdatasets.torch_dataset import Dataset as torch_Dataset
from pytorch_lightning import Trainer
from scipy import stats
from sklearn.metrics.pairwise import paired_distances
from torch.utils.data import DataLoader, IterableDataset
from tqdm import tqdm
from transformers import HfArgumentParser, BertTokenizer

train_info_args = {
    'devices':  1,
    'data_backend': 'record',
    'model_type': 'bert',
    'model_name_or_path': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'tokenizer_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese',
    'config_name': '/data/nlp/pre_models/torch/bert/bert-base-chinese/config.json',
    'do_train': True,
    'do_eval': True,
    # 'train_file':'/data/nlp/nlp_train_data/clue/afqmc_public/train.json',
    # 'eval_file':'/data/nlp/nlp_train_data/clue/afqmc_public/dev.json',
    # 'test_file':'/data/nlp/nlp_train_data/clue/afqmc_public/test.json',
    # 'train_file':'/data/nlp/nlp_train_data/senteval_cn/LCQMC/LCQMC.train.data',
    # 'eval_file':'/data/nlp/nlp_train_data/senteval_cn/LCQMC/LCQMC.valid.data',
    # 'test_file':'/data/nlp/nlp_train_data/senteval_cn/LCQMC/LCQMC.test.data',
    'train_file':'/data/nlp/nlp_train_data/senteval_cn/STS-B/STS-B.train.data',
    'eval_file':'/data/nlp/nlp_train_data/senteval_cn/STS-B/STS-B.valid.data',
    'test_file':'/data/nlp/nlp_train_data/senteval_cn/STS-B/STS-B.test.data',
    # 'train_file': '/data/nlp/nlp_train_data/senteval_cn/BQ/BQ.train.data',
    # 'eval_file': '/data/nlp/nlp_train_data/senteval_cn/BQ/BQ.valid.data',
    # 'test_file': '/data/nlp/nlp_train_data/senteval_cn/BQ/BQ.test.data',
    # 'train_file':'/data/nlp/nlp_train_data/senteval_cn/ATEC/ATEC.train.data',
    # 'eval_file':'/data/nlp/nlp_train_data/senteval_cn/ATEC/ATEC.valid.data',
    # 'test_file':'/data/nlp/nlp_train_data/senteval_cn/ATEC/ATEC.test.data',
    'max_epochs': 1,
    'optimizer': 'adamw',
    'learning_rate':1e-5,
    'train_batch_size': 40,
    'eval_batch_size': 20,
    'test_batch_size': 2,
    'adam_epsilon': 1e-8,
    'gradient_accumulation_steps': 1,
    'max_grad_norm': 1.0,
    'weight_decay': 0,
    'warmup_steps': 0,
    'output_dir': './output',
    'train_max_seq_length': 80,
    'eval_max_seq_length': 80,
    'test_max_seq_length': 80,
    #模型参数
    'pooling_with_mlp': False,
}


SENTENCE_TEMPLATE = "*cls*_This_sentence_:_\'*sent_0*\'_means*mask*.*sep+*"
SENTENCE_TEMPLATE2= "*cls*_This_sentence_:_\"*sent_0*\"_means*mask*.*sep+*"

def process_template(template,mask_token = '[MASK]'):
    template = template.replace('*mask*', mask_token) \
        .replace('*sep+*', '') \
        .replace('*cls*', '').replace('*sent_0*', ' ')
    template = template.split(' ')
    return template[0].replace('_', ' '),template[1].replace('_', ' ')
# This sentence : ' ' means[MASK].
mask_embedding_sentence_bs,mask_embedding_sentence_es = process_template(SENTENCE_TEMPLATE)
mask_embedding_sentence_bs2,mask_embedding_sentence_es2 = process_template(SENTENCE_TEMPLATE)

class NN_DataHelper(DataHelper):
    index = 1

    def on_data_ready(self):
        self.index = -1

    # 切分词
    def on_data_process(self, data: typing.Any, user_data: tuple):
        self.index += 1
        tokenizer: BertTokenizer
        tokenizer, max_seq_length, do_lower_case, label2id, mode = user_data
        sentence1, sentence2, label_str = data
        if mode == 'train':
            ds = []
            for sentence in [sentence1, sentence2]:
                sentence = sentence.replace('[MASK]','')
                pair = []
                for sentence, mask_bs, mask_es in [(sentence, mask_embedding_sentence_bs, mask_embedding_sentence_es),
                                                   (sentence, mask_embedding_sentence_bs2, mask_embedding_sentence_es2)]:

                    o = tokenizer(mask_bs + sentence[:max_seq_length - len(mask_bs) - len(mask_es)] + mask_es, max_length=max_seq_length, truncation=True, add_special_tokens=True,return_token_type_ids=False)
                    for k in o:
                        o[k] = np.asarray(o[k],dtype=np.int32)
                    seqlen = np.asarray(len(o['input_ids']), dtype=np.int32)
                    pad_len = max_seq_length - seqlen
                    if pad_len > 0:
                        pad_val = tokenizer.pad_token_id
                        o['input_ids'] = np.pad(o['input_ids'], pad_width=(0, pad_len), constant_values=(pad_val, pad_val))
                        o['attention_mask'] = np.pad(o['attention_mask'], pad_width=(0, pad_len), constant_values=(0, 0))
                    d = {
                        'input_ids': o['input_ids'],
                        'attention_mask': o['attention_mask'],
                        'seqlen': seqlen
                    }
                    pair.append(copy.deepcopy(d))
                seqlen = np.max([p['seqlen'] for p in pair])
                d = {k:[] for k in pair[0].keys()}
                for node in pair:
                    for k,v in node.items():
                        d[k].append(v)
                d = {k: np.stack(v,axis=0) for k,v in d.items()}
                d['seqlen'] = np.asarray(seqlen,dtype=np.int32)
                ds.append(copy.deepcopy(d))
            return ds
        #验证
        else:
            ds = {}
            sentence1 = sentence1.replace('[MASK]', '')
            sentence2 = sentence2.replace('[MASK]', '')
            for sentence,mask_bs,mask_es in [(sentence1,mask_embedding_sentence_bs, mask_embedding_sentence_es),
                                            (sentence2,mask_embedding_sentence_bs2, mask_embedding_sentence_es2)]:
                o = tokenizer(mask_bs + sentence[:max_seq_length - len(mask_bs) - len(mask_es)] + mask_es, max_length=max_seq_length, truncation=True, add_special_tokens=True,return_token_type_ids=False)
                for k in o:
                    o[k] = np.asarray(o[k],dtype=np.int32)
                seqlen = np.asarray(len(o['input_ids']), dtype=np.int32)
                pad_len = max_seq_length - seqlen
                if pad_len > 0:
                    pad_val = tokenizer.pad_token_id
                    o['input_ids'] = np.pad(o['input_ids'], pad_width=(0, pad_len), constant_values=(pad_val, pad_val))
                    o['attention_mask'] = np.pad(o['attention_mask'], pad_width=(0, pad_len), constant_values=(0, 0))
                d = {
                    'input_ids': o['input_ids'],
                    'attention_mask': o['attention_mask'],
                    'seqlen': seqlen
                }
                if 'input_ids' not in ds:
                    ds = d
                else:
                    for k,v in d.items():
                        ds[k+'2'] = v
            if label_str is not None:
                labels = np.asarray(int(label_str), dtype=np.int32)
                ds['labels'] = labels
            return ds


    # 读取标签
    def on_get_labels(self, files: typing.List[str]):
        return None,None

    # 读取文件
    def on_get_corpus(self, files: typing.List, mode: str):
        D = []
        for filename in files:
            with open(filename, mode='r', encoding='utf-8') as f:
                lines = f.readlines()
                if filename.endswith('.json'):
                    for line in lines:
                        jd = json.loads(line)
                        if not jd:
                            continue
                        D.append((jd['sentence1'], jd['sentence2'], jd.get('label', None)))
                else:
                    for line in lines:
                        line = line.replace('\r\n', '').replace('\n', '')
                        s1, s2, l = line.split('\t', 2)
                        D.append((s1, s2, l))
        # 训练数据重排序
        if mode == 'train':
            tmp = []
            for item in D:
                tmp.append(item[0])
                tmp.append(item[1])
            random.shuffle(tmp)
            D.clear()
            for item1,item2 in zip(tmp[::2], tmp[1::2]):
                D.append((item1,item2,None))
        return D

    @staticmethod
    def train_collate_fn(batch):
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
        o['input_ids'] = o['input_ids'][:,:, :max_len]
        o['attention_mask'] = o['attention_mask'][:,:, :max_len]
        return o

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
        if 'seqlen2' in o:
            max_len = torch.max(o.pop('seqlen2'))
            o['input_ids2'] = o['input_ids2'][:, :max_len]
            o['attention_mask2'] = o['attention_mask2'][:, :max_len]
        return o



class MyTransformer(TransformerForPromptbertcse, with_pl=True):
    def __init__(self, *args, **kwargs):
        super(MyTransformer, self).__init__(*args, **kwargs)


def evaluate_sample(a_vecs,b_vecs,labels):
    print('*' * 30,'evaluating....',a_vecs.shape,b_vecs.shape,labels.shape)
    sims = 1 - paired_distances(a_vecs,b_vecs,metric='cosine')
    print(np.concatenate([sims[:5] , sims[-5:]],axis=0))
    print(np.concatenate([labels[:5] , labels[-5:]],axis=0))
    correlation,_  = stats.spearmanr(labels,sims)
    print('spearman ', correlation)
    return correlation

class MySimpleModelCheckpoint(SimpleModelCheckpoint):
    def __init__(self, *args, **kwargs):
        super(MySimpleModelCheckpoint, self).__init__(*args, **kwargs)
        self.weight_file = './best.pt'

    def on_save_model(
            self, trainer: "pl.Trainer", pl_module: "pl.LightningModule"
    ) -> None:
        pl_module: MyTransformer

        # 当前设备
        device = torch.device('cuda:{}'.format(trainer.global_rank))
        eval_datasets = dataHelper.load_dataset(dataHelper.eval_files)
        eval_datasets = DataLoader(eval_datasets, batch_size=training_args.eval_batch_size,collate_fn=dataHelper.collate_fn)

        a_vecs, b_vecs, labels = [], [], []
        for i, batch in tqdm(enumerate(eval_datasets), total=len(eval_datasets), desc='evalute'):
            for k in batch:
                batch[k] = batch[k].to(device)
            o = pl_module.validation_step(batch, i)
            a_logits, b_logits, b_labels = o['outputs']
            for j in range(len(b_logits)):
                logit1 = np.asarray(a_logits[j], dtype=np.float32)
                logit2 = np.asarray(b_logits[j], dtype=np.float32)
                label = np.asarray(b_labels[j], dtype=np.int32)

                a_vecs.append(logit1)
                b_vecs.append(logit2)
                labels.append(label)

        a_vecs = np.stack(a_vecs, axis=0)
        b_vecs = np.stack(b_vecs, axis=0)
        labels = np.stack(labels, axis=0)

        corrcoef = evaluate_sample(a_vecs, b_vecs, labels)
        f1 = corrcoef
        best_f1 = self.best.get('f1',-np.inf)
        print('current', f1, 'best', best_f1)
        if f1 >= best_f1:
            self.best['f1'] = f1
            logging.info('save best {}, {}\n'.format(self.best['f1'], self.weight_file))
            trainer.save_checkpoint(self.weight_file)

if __name__ == '__main__':
    parser = HfArgumentParser((ModelArguments, TrainingArguments, DataArguments,PromptBertcseArguments))
    model_args, training_args, data_args,promptbertcse_args = parser.parse_dict(train_info_args)
    checkpoint_callback = MySimpleModelCheckpoint(monitor="f1", every_n_epochs=1,
                                                  every_n_train_steps=300 // training_args.gradient_accumulation_steps)
    trainer = Trainer(
        log_every_n_steps=20,
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
        strategy='ddp' if torch.cuda.device_count() > 1 else None,
    )

    dataHelper = NN_DataHelper(data_args.data_backend)
    tokenizer, config, label2id, id2label = load_tokenizer_and_config_with_args(dataHelper, model_args, training_args,
                                                                                data_args)
    rng = random.Random(training_args.seed)

    token_fn_args_dict = {
        'train': (tokenizer, data_args.train_max_seq_length, model_args.do_lower_case, label2id,
                  'train'),
        'eval': (tokenizer, data_args.eval_max_seq_length, model_args.do_lower_case, label2id,
                 'eval'),
        'test': (tokenizer, data_args.test_max_seq_length, model_args.do_lower_case, label2id,
                 'test')
    }

    config.task_specific_params['mask_token_id'] = tokenizer.mask_token_id

    # 缓存数据集
    intermediate_name = data_args.intermediate_name + '_{}'.format(0)
    if data_args.do_train:
        dataHelper.train_files.append(
            dataHelper.make_dataset_with_args(data_args.train_file, token_fn_args_dict['train'],
                                              data_args,
                                              intermediate_name=intermediate_name, shuffle=True,
                                              mode='train'))
    if data_args.do_eval:
        dataHelper.eval_files.append(dataHelper.make_dataset_with_args(data_args.eval_file, token_fn_args_dict['eval'],
                                                                       data_args,
                                                                       intermediate_name=intermediate_name,
                                                                       shuffle=False,
                                                                       mode='eval'))
    if data_args.do_test:
        dataHelper.test_files.append(dataHelper.make_dataset_with_args(data_args.test_file, token_fn_args_dict['test'],
                                                                       data_args,
                                                                       intermediate_name=intermediate_name,
                                                                       shuffle=False,
                                                                       mode='test'))

    train_datasets = dataHelper.load_dataset(dataHelper.train_files, shuffle=True, num_processes=trainer.world_size,
                                             process_index=trainer.global_rank, infinite=True,
                                             with_record_iterable_dataset=False,
                                             with_load_memory=True,with_torchdataset=False)


    if train_datasets is not None:
        # 随机选出一万训练数据
        train_datasets = torch_Dataset(train_datasets.limit(20000))
        train_datasets = DataLoader(train_datasets, batch_size=training_args.train_batch_size,
                                    collate_fn=dataHelper.train_collate_fn,
                                    shuffle=False if isinstance(train_datasets, IterableDataset) else True)

    # 修改config的dropout系数
    config.attention_probs_dropout_prob = 0.3
    config.hidden_dropout_prob = 0.3
    model = MyTransformer(promptbertcse_args=promptbertcse_args,config=config, model_args=model_args, training_args=training_args)

    if train_datasets is not None:
        trainer.fit(model, train_dataloaders=train_datasets)
    else:
        eval_datasets = dataHelper.load_dataset(dataHelper.eval_files)
        test_datasets = dataHelper.load_dataset(dataHelper.test_files)
        if eval_datasets is not None:
            eval_datasets = DataLoader(eval_datasets, batch_size=training_args.eval_batch_size,
                                       collate_fn=dataHelper.collate_fn)
        if test_datasets is not None:
            test_datasets = DataLoader(test_datasets, batch_size=training_args.test_batch_size,
                                       collate_fn=dataHelper.collate_fn)
        if eval_datasets is not None:
            trainer.validate(model, dataloaders=eval_datasets,ckpt_path='./best.pt')

        if test_datasets is not None:
            trainer.test(model, dataloaders=test_datasets,ckpt_path='best.pt')