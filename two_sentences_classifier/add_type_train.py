"""BERT finetuning runner."""

from __future__ import absolute_import, division, print_function

import argparse
import logging
import os
import random
import sys
from collections import defaultdict
from itertools import chain

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import classification_report
from torch.utils.data import DataLoader, RandomSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

sys.path.append("../common_file")

import tokenization
from modeling import BertConfig, RelationClassifier, TwoSentenceClassifier
from optimization import BertAdam
from parallel import BalancedDataParallel


logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

WEIGHTS_NAME = 'pytorch_model.bin'
CONFIG_NAME = 'bert_config.json'


class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, id, text_a, text_b=None, label=None, name=None, person=None):
        self.id = id
        self.text_a = text_a
        self.text_b = text_b
        self.label = label
        self.name = name
        self.person = person


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 input_ids,
                 input_mask,
                 segment_ids,
                 label_id,
                 position_ids=None,
                 example_id=None):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.example_id = example_id
        self.position_ids = position_ids


class DataProcessor(object):
    """Processor for the CoLA data set (GLUE version)."""

    def __init__(self, num_labels):
        self.num_labels = num_labels
        self.map_symbols = {"’": "'", "‘": "'", "“": '"', "”": '"'}

    def read_novel_examples(self, path, top_n=7, task_name='train'):
        top_n = int(top_n)
        examples = []
        example_map_ids = {}
        zero, one, index, add_one_data = 0, 0, 0, 0
        para_nums_counter = defaultdict(int)
        with open(path, 'r', encoding='utf-8') as f:
            for _, line in enumerate(f):
                line = line.replace('\n', '').split('\t')
                paras = [self.clean_text(p) for p in line[:2]]
                assert len(paras) == 2
                text_a = paras[0].split('||')
                text_b = paras[1].split('||')

                if len(text_a) < top_n or len(text_b) < top_n:
                    continue
                para_nums_counter[len(text_a)] += 1
                para_nums_counter[len(text_b)] += 1
                label = int(line[2])
                # person_names = line[-1].split('||')
                # if person_names[0] != person_names[1] and label:
                #     continue
                assert label in [0, 1]
                if label:
                    one += 1
                else:
                    zero += 1
                example = InputExample(id=index,
                                       text_a=text_a[:top_n],
                                       text_b=text_b[:top_n],
                                       label=label,
                                       name=line[-2],
                                       person=line[-1])
                examples.append(example)

                if task_name != 'train':
                    example_map_ids[index] = example

                # if len(text_a) >= 2 * top_n and len(
                #         text_b) >= 2 * top_n and task_name == 'train':
                #     index += 1
                #     add_one_data += 1
                #     example = InputExample(id=index,
                #                            text_a=text_a[top_n:2 * top_n],
                #                            text_b=text_b[top_n:2 * top_n],
                #                            label=label,
                #                            name=line[-2],
                #                            person=line[-1])
                #     examples.append(example)
                #     example_map_ids[index] = example
                index += 1

        print(f'{os.path.split(path)[-1]} file examples {len(examples)}')
        print(
            f'zero = {zero}, one = {one}  add_one_data = {add_one_data} \n para_nums_counter= {para_nums_counter}'
        )
        return examples, example_map_ids

    def read_file_dir(self, dir, top_n=7):
        all_examples = []
        for root, path_dir, file_names in os.walk(dir):
            for file_name in file_names:
                # if file_name.endswith('001'):
                file_abs_path = os.path.join(root, file_name)
                examples, _ = self.read_novel_examples(file_abs_path, top_n=top_n)
                all_examples.extend(examples)
        print(f'dir all file  examples {len(all_examples)}')
        return all_examples

    def clean_text(self, text):
        text = [self.map_symbols.get(w) if self.map_symbols.get(w) else w for w in text]
        return ''.join(text)


def convert_examples_to_features(examples, max_seq_length, tokenizer):
    """Loads a data file into a list of `InputBatch`s."""
    features = []
    sent_length_counter = defaultdict(int)
    for (ex_index, example) in enumerate(examples):
        input_ids, input_masks, segment_ids = [], [], []
        min_length = min(len(example.text_b), len(example.text_a))
        text_a = example.text_a[:min_length]
        text_b = example.text_b[:min_length]
        for i, sent in enumerate(chain(text_a, text_b)):
            sent_length = len(sent)
            sents = sent.split('""')
            if len(sents) != 2:
                break
            sents[0] = sents[0][:120].replace('"', '')
            sents[1] = sents[1][:120].replace('"', '')
            sents_token = [tokenizer.tokenize(s) for s in sents]
            sent_segment_ids = [0] * (len(sents_token[0]) + 2) + [1] * (len(sents_token[1]) + 1)
            sents_token = sents_token[0] + ['[SEP]'] + sents_token[1]
            sents_token = sents_token[:max_seq_length - 2]
            sent_segment_ids = sent_segment_ids[:max_seq_length]
            sents_token = ['[CLS]'] + sents_token + ['[SEP]']
            if 100 > sent_length >= 50:
                sent_length_counter['100>50'] += 1
            elif 50 > sent_length:
                sent_length_counter['<50'] += 1
            elif 180 > sent_length >= 100:
                sent_length_counter['180>100'] += 1
            else:
                sent_length_counter['>180'] += 1
            length = len(sents_token)
            sent_input_masks = [1] * length
            sent_input_ids = tokenizer.convert_tokens_to_ids(sents_token)

            while length < max_seq_length:
                sent_input_ids.append(0)
                sent_input_masks.append(0)
                sent_segment_ids.append(0)
                length += 1

            assert len(sent_segment_ids) == len(sent_input_ids) == len(sent_input_masks)
            input_ids.append(sent_input_ids)
            input_masks.append(sent_input_masks)
            segment_ids.append(sent_segment_ids)
        if len(input_ids) != 14:
            print(f'len {len(input_ids)}')
            continue

        features.append(
            InputFeatures(input_ids=input_ids,
                          input_mask=input_masks,
                          segment_ids=segment_ids,
                          label_id=example.label,
                          example_id=example.id))
    print(f'feature example input_ids：{features[-1].input_ids}')
    print(f'feature example input_mask：{features[-1].input_mask}')
    print(f'feature example segment_ids：{features[-1].segment_ids}')

    print(f'total features {len(features)}')
    print(sent_length_counter)
    return features


def main():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument("--train_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The train file path")
    parser.add_argument("--eval_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The dev file path")
    parser.add_argument("--eval_train_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The train  eval file path")
    parser.add_argument("--predict_file",
                        default=None,
                        type=str,
                        required=False,
                        help="The predict file path")
    parser.add_argument("--top_n",
                        default=5,
                        type=int,
                        required=True,
                        help="higher than threshold is classify 1,")
    parser.add_argument("--reduce_dim",
                        default=64,
                        type=int,
                        help="from hidden size to this dimensions, reduce dim")
    parser.add_argument("--bert_config_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The config json file corresponding to the pre-trained BERT model. \n"
                        "This specifies the model architecture.")
    parser.add_argument("--bert_model",
                        default=None,
                        type=str,
                        required=True,
                        help="The config json file corresponding to the pre-trained BERT model. \n"
                        "This specifies the model architecture.")
    parser.add_argument("--result_file",
                        default=None,
                        type=str,
                        required=False,
                        help="The result file that the BERT model was trained on.")
    parser.add_argument("--vocab_file",
                        default=None,
                        type=str,
                        required=True,
                        help="The vocabulary file that the BERT model was trained on.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model checkpoints will be written.")
    # Other parameters
    parser.add_argument("--init_checkpoint",
                        default=None,
                        type=str,
                        help="Initial checkpoint (usually from a pre-trained BERT model).")
    parser.add_argument("--do_lower_case",
                        default=False,
                        action='store_true',
                        help="Whether to lower case the input text.")
    parser.add_argument("--max_seq_length",
                        default=180,
                        type=int,
                        help="maximum total input sequence length after WordPiece tokenization.")
    parser.add_argument("--gpu0_size",
                        default=1,
                        type=int,
                        help="maximum total input sequence length after WordPiece tokenization.")
    parser.add_argument("--do_train",
                        default=False,
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_predict",
                        default=False,
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--num_labels", default=1, type=int, help="mapping classify nums")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size", default=8, type=int, help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=6.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                        "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--save_checkpoints_steps",
                        default=1000,
                        type=int,
                        help="How often to save the model checkpoint.")
    parser.add_argument("--no_cuda",
                        default=False,
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed', type=int, default=42, help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumualte before")
    parser.add_argument('--optimize_on_cpu',
                        default=False,
                        action='store_true',
                        help="Whether to perform optimization and averages on CPU")
    parser.add_argument('--fp16',
                        default=False,
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float,
                        default=128,
                        help='Loss scale, positive power of 2 can improve fp16 convergence.')

    args = parser.parse_args()

    data_processor = DataProcessor(args.num_labels)
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
        if args.fp16:
            logger.info("16-bits training currently not supported in distributed training")
            args.fp16 = False    # (see https://github.com/pytorch/pytorch/pull/13496)
    logger.info("device %s n_gpu %d distributed training %r", device, n_gpu,
                bool(args.local_rank != -1))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = int(args.train_batch_size / args.gradient_accumulation_steps)

    print(f'args.train_batch_size = {args.train_batch_size}')
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_predict:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    bert_config = BertConfig.from_json_file(args.bert_config_file)
    bert_config.reduce_dim = args.reduce_dim


    if args.max_seq_length > bert_config.max_position_embeddings:
        raise ValueError(
            "Cannot use sequence length {} because the BERT model was only trained up to sequence length {}"
            .format(args.max_seq_length, bert_config.max_position_embeddings))

    if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train:
        raise ValueError("Output directory ({}) already exists and is not empty.".format(
            args.output_dir))

    if args.do_train:
        os.makedirs(args.output_dir, exist_ok=True)

    tokenizer = tokenization.FullTokenizer(vocab_file=args.vocab_file,
                                           do_lower_case=args.do_lower_case)

    def prepare_data(args, task_name='train'):
        if task_name == 'train':
            file_path = args.train_file
        elif task_name == 'eval':
            file_path = args.eval_file
        elif task_name == 'train_eval':
            file_path = args.eval_train_file

        if os.path.isdir(file_path):
            examples = data_processor.read_file_dir(file_path, top_n=args.top_n)
        else:
            examples, example_map_ids = data_processor.read_novel_examples(file_path,
                                                                           top_n=args.top_n,
                                                                           task_name=task_name)
        features = convert_examples_to_features(examples, args.max_seq_length, tokenizer)
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        all_example_ids = torch.tensor([f.example_id for f in features], dtype=torch.long)

        if task_name in ['train', 'eval', 'train_eval']:
            all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
            datas = TensorDataset(all_example_ids, all_input_ids, all_input_mask, all_segment_ids,
                                  all_label_ids)
        else:
            datas = TensorDataset(all_example_ids, all_input_ids, all_input_mask, all_segment_ids)

        if task_name == 'train':
            if args.local_rank == -1:
                data_sampler = RandomSampler(datas)
            else:
                data_sampler = DistributedSampler(datas)
            dataloader = DataLoader(datas,
                                    sampler=data_sampler,
                                    batch_size=args.train_batch_size,
                                    drop_last=True)
        else:
            dataloader = DataLoader(datas, batch_size=args.eval_batch_size, drop_last=True)
        return (dataloader, example_map_ids) if task_name != 'train' else dataloader

    def accuracy(example_ids, logits, probs=None, data_type='eval'):
        logits = logits.tolist()
        example_ids = example_ids.tolist()
        assert len(logits) == len(example_ids)
        classify_name = ['no_answer', 'yes_answer']
        labels, text_a, text_b, novel_names, persons = [], [], [], [], []
        map_dicts = example_map_ids if data_type == 'eval' else train_example_map_ids
        for i in example_ids:
            example = map_dicts[i]
            labels.append(example.label)
            text_a.append("||".join(example.text_a))
            text_b.append("||".join(example.text_b))
            persons.append(example.person)
            novel_names.append(example.name)

        write_data = pd.DataFrame({
            "text_a": text_a,
            "text_b": text_b,
            "labels": labels,
            "logits": logits,
            "novel_names": novel_names,
            "person": persons
        })
        write_data['yes_or_no'] = write_data['labels'] == write_data['logits']
        if probs is not None:
            write_data['logits'] = probs.tolist()
        write_data.to_csv(args.result_file, index=False)
        assert len(labels) == len(logits)
        result = classification_report(labels, logits, target_names=classify_name)
        return result

    def eval_model(model, eval_dataloader, device, data_type='eval'):
        model.eval()
        eval_loss = 0
        all_logits = []
        all_example_ids = []
        all_probs = []
        accuracy_result = None
        batch_count = 0
        for step, batch in enumerate(tqdm(eval_dataloader, desc="evaluating")):
            example_ids, input_ids, input_mask, segment_ids, label_ids = batch
            if not args.do_train:
                label_ids = None
            with torch.no_grad():
                tmp_eval_loss, logits = model(input_ids, segment_ids, input_mask, labels=label_ids)
                argmax_logits = torch.argmax(logits, dim=1)
                first_indices = torch.arange(argmax_logits.size()[0])
                logits_probs = logits[first_indices, argmax_logits]
            if args.do_train:
                eval_loss += tmp_eval_loss.mean().item()
                all_logits.append(argmax_logits)
                all_example_ids.append(example_ids)
            else:
                all_logits.append(argmax_logits)
                all_example_ids.append(example_ids)
                all_probs.append(logits_probs)
            batch_count += 1
        if all_logits:
            all_logits = torch.cat(all_logits, dim=0)
            all_example_ids = torch.cat(all_example_ids, dim=0)
            all_probs = torch.cat(all_probs, dim=0) if len(all_probs) else None
            accuracy_result = accuracy(all_example_ids,
                                       all_logits,
                                       probs=all_probs,
                                       data_type=data_type)
        eval_loss /= batch_count
        print(f'========= {data_type} acc ============\n')
        print(f'{accuracy_result}')
        return eval_loss, accuracy_result, all_logits

    train_dataloader = None
    num_train_steps = None
    if args.do_train:
        train_dataloader = prepare_data(args, task_name='train')
        num_train_steps = int(
            len(train_dataloader) / args.gradient_accumulation_steps * args.num_train_epochs)

    model = RelationClassifier(bert_config, num_labels=data_processor.num_labels)
    # model = TwoSentenceClassifier(bert_config, num_labels=data_processor.num_labels)

    new_state_dict = model.state_dict()
    init_state_dict = torch.load(os.path.join(args.bert_model, 'pytorch_model.bin'))
    for k, v in init_state_dict.items():
        if k in new_state_dict:
            print(f'k in = {k} v in shape = {v.shape}')
            new_state_dict[k] = v
    model.load_state_dict(new_state_dict)

    for k, v in model.state_dict().items():
        print(f'k = {k}, v shape {v.shape}')

    if args.fp16:
        model.half()

    if args.do_predict:
        model_path = os.path.join(args.output_dir, WEIGHTS_NAME)
        new_state_dict = torch.load(model_path)
        new_state_dict = dict([
            (k[7:], v) if k.startswith('module') else (k, v) for k, v in new_state_dict.items()
        ])
        model.load_state_dict(new_state_dict)

    model.to(device)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(model,
                                                          device_ids=[args.local_rank],
                                                          output_device=args.local_rank)
    elif n_gpu > 1:
        if args.gpu0_size > 0:
            model = BalancedDataParallel(args.gpu0_size, model, dim=0).to(device)
        else:
            model = torch.nn.DataParallel(model)

    if args.fp16:
        param_optimizer = [(n, param.clone().detach().to('cpu').float().requires_grad_())
                           for n, param in model.named_parameters()]
    elif args.optimize_on_cpu:
        param_optimizer = [(n, param.clone().detach().to('cpu').requires_grad_())
                           for n, param in model.named_parameters()]
    else:
        param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'gamma', 'beta']
    optimizer_grouped_parameters = [{
        'params': [p for n, p in param_optimizer if n not in no_decay],
        'weight_decay': 0.01
    }, {
        'params': [p for n, p in param_optimizer if n in no_decay],
        'weight_decay': 0.0
    }]

    eval_dataloader, example_map_ids = prepare_data(args, task_name='eval')
    train_eval_dataloader, train_example_map_ids = prepare_data(args, task_name='train_eval')

    if args.do_train:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=num_train_steps)

        output_config_file = os.path.join(args.output_dir, CONFIG_NAME)
        eval_loss, acc, _ = eval_model(model, eval_dataloader, device)
        logger.info(f'初始开发集loss: {eval_loss}')
        for epoch in trange(int(args.num_train_epochs), desc="Epoch"):
            model.train()
            torch.cuda.empty_cache()
            model_save_path = os.path.join(args.output_dir, f"{WEIGHTS_NAME}.{epoch}")
            tr_loss = 0
            train_batch_count = 0
            for step, batch in enumerate(tqdm(train_dataloader, desc="training")):
                _, input_ids, input_mask, segment_ids, label_ids = batch
                loss, _ = model(input_ids, segment_ids, input_mask, labels=label_ids)
                if n_gpu > 1:
                    loss = loss.mean()
                if args.fp16 and args.loss_scale != 1.0:
                    loss = loss * args.loss_scale
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                loss.backward()
                tr_loss += loss.item()
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    optimizer.step()
                    model.zero_grad()
                train_batch_count += 1
            tr_loss /= train_batch_count
            eval_loss, acc, _ = eval_model(model, eval_dataloader, device)
            eval_model(model, train_eval_dataloader, device, data_type='train_eval')
            logger.info(
                f'训练loss: {tr_loss}, 开发集loss：{eval_loss} 训练轮数：{epoch + 1}/{int(args.num_train_epochs)}'
            )
            model_to_save = model.module if hasattr(model, 'module') else model
            torch.save(model.state_dict(), model_save_path)
            if epoch == 0:
                model_to_save.config.to_json_file(output_config_file)
                tokenizer.save_vocabulary(args.output_dir)

    if args.do_predict:
        # eval_model(model, train_eval_dataloader, device, data_type='train_eval')
        eval_model(model, eval_dataloader, device, data_type='eval')


if __name__ == "__main__":
    main()
