from __future__ import division

import datetime
import gc
import inspect
import math
import os
import re
import time
import torch
from apex import amp

import onmt
import onmt.markdown
import onmt.modules
from onmt.data.data_iterator import DataIterator
from onmt.data.dataset import rewrap
from onmt.model_factory import build_model, build_language_model, optimize_model
from onmt.model_factory import init_model_parameters
from onmt.train_utils.stats import Logger
from onmt.utils import checkpoint_paths, normalize_gradients


def varname(p):
    for line in inspect.getframeinfo(inspect.currentframe().f_back)[3]:
        m = re.search(r'\bvarname\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', line)
        if m:
            return m.group(1)


class BaseTrainer(object):

    def __init__(self, model, loss_function, train_data, valid_data, dicts, opt):

        self.model = model
        self.train_data = train_data
        self.valid_data = valid_data

        self.dicts = dicts
        self.opt = opt
        self.cuda = (len(opt.gpus) >= 1 and opt.gpus[0] >= 0)

        self.loss_function = loss_function
        self.start_time = 0

        self.additional_data = []
        self.additional_data_ratio = []

    def add_additional_data(self, d, ratio):
        self.additional_data = d
        if ratio == "-1":
            self.additional_data_ratio = [1] * (len(self.additional_data + 1))
        else:
            self.additional_data_ratio = [int(s) for s in ratio.split(";")]
            assert (len(self.additional_data_ratio) == len(self.additional_data) + 1)

    def run(self, *args, **kwargs):

        raise NotImplementedError

    def eval(self, data):

        raise NotImplementedError

    def load_encoder_weight(self, checkpoint_file):

        print("Loading pretrained models from %s" % checkpoint_file)
        checkpoint = torch.load(checkpoint_file, map_location=lambda storage, loc: storage)

        pretrained_model = build_model(checkpoint['opt'], checkpoint['dicts'])
        pretrained_model.load_state_dict(checkpoint['model'])

        print("Loading pretrained encoder weights ...")
        pretrained_model.encoder.language_embedding = None
        enc_language_embedding = self.model.encoder.language_embedding
        self.model.encoder.language_embedding = None
        encoder_state_dict = pretrained_model.encoder.state_dict()

        self.model.encoder.load_state_dict(encoder_state_dict)
        self.model.encoder.language_embedding = enc_language_embedding
        return

    def load_decoder_weight(self, checkpoint_file):

        print("Loading pretrained models from %s" % checkpoint_file)
        checkpoint = torch.load(checkpoint_file, map_location=lambda storage, loc: storage)
        chkpoint_dict = checkpoint['dicts']

        pretrained_model = build_model(checkpoint['opt'], chkpoint_dict)
        pretrained_model.load_state_dict(checkpoint['model'])

        print("Loading pretrained decoder weights ...")
        # first we have to remove the embeddings which probably have difference size ...
        pretrained_word_emb = pretrained_model.decoder.word_lut
        pretrained_model.decoder.word_lut = None
        pretrained_lang_emb = pretrained_model.decoder.language_embeddings
        pretrained_model.decoder.language_embeddings = None

        # actually we assume that two decoders have the same language embeddings... 
        untrained_word_emb = self.model.decoder.word_lut
        self.model.decoder.word_lut = None
        untrained_lang_emb = self.model.decoder.language_embeddings
        self.model.decoder.language_embeddings = None

        decoder_state_dict = pretrained_model.decoder.state_dict()
        self.model.decoder.load_state_dict(decoder_state_dict)

        # now we load the embeddings ....
        n_copies = 0
        for token in self.dicts['tgt'].labelToIdx:

            untrained_id = self.dicts['tgt'].labelToIdx[token]

            if token in chkpoint_dict['tgt'].labelToIdx:
                pretrained_id = chkpoint_dict['tgt'].labelToIdx[token]
                untrained_word_emb.weight.data[untrained_id].copy_(pretrained_word_emb.weight.data[pretrained_id])

                self.model.generator[0].linear.bias.data[untrained_id].copy_(pretrained_model
                                                                             .generator[0].linear.bias.data[
                                                                                 pretrained_id])
                n_copies += 1

        print("Copied embedding for %d words" % n_copies)
        self.model.decoder.word_lut = untrained_word_emb

        # now we load the language embeddings ...
        if pretrained_lang_emb and untrained_lang_emb and 'langs' in chkpoint_dict:
            for lang in self.dicts['langs']:

                untrained_id = self.dicts['langs'][lang]
                if lang in chkpoint_dict['langs']:
                    pretrained_id = chkpoint_dict['langs'][lang]
                    untrained_lang_emb.weight.data[untrained_id].copy_(pretrained_lang_emb.weight.data[pretrained_id])

        self.model.decoder.language_embeddings = untrained_lang_emb

    def _get_grads(self):
        grads = []
        for name, p in self.model.named_parameters():
            if not p.requires_grad:
                continue
            if p.grad is None:
                raise RuntimeError('Model parameter did not receive gradient: ' + name + '. '
                                                                                         'Use the param in the forward pass or set requires_grad=False.' +
                                   ' If you are using Stochastic model + fp16 - '
                                   'try to increase the number of minibatches' +
                                   ' each update to avoid uninitialized gradients.')
            grads.append(p.grad.data)
        return grads

    def _get_flat_grads(self, out=None):
        grads = self._get_grads()
        if out is None:
            grads_size = sum(g.numel() for g in grads)
            out = grads[0].new(
                grads_size).zero_()
        offset = 0
        for g in grads:
            numel = g.numel()
            out[offset:offset + numel].copy_(g.view(-1))
            offset += numel
        return out[:offset]

    def warm_up(self):
        """
        Warmup the memory allocator, by attempting to fit the largest batch
        :return:
        """
        if self.opt.memory_profiling:
            from pytorch_memlab import MemReporter
            reporter = MemReporter()

        batch = self.train_data.get_largest_batch()
        opt = self.opt
        denom = 32000

        if self.cuda:
            batch.cuda(fp16=self.opt.fp16 and not self.opt.fp16_mixed)

        self.model.train()
        self.model.zero_grad()
        oom = False

        if self.opt.memory_profiling:
            print("Input size: ")
            print(batch.size, batch.src_size, batch.tgt_size)

        if opt.streaming:
            streaming_state = self.model.init_stream()
        else:
            streaming_state = None

        try:
            targets = batch.get('target_output')
            tgt_mask = targets.data.ne(onmt.constants.PAD)
            outputs = self.model(batch, streaming=opt.streaming, target_mask=tgt_mask,
                                 zero_encoder=opt.zero_encoder,
                                 mirror=opt.mirror_loss, streaming_state=streaming_state)

            outputs['tgt_mask'] = tgt_mask

            loss_dict = self.loss_function(outputs, targets, model=self.model)
            loss = loss_dict['loss'].div(denom)  # a little trick to avoid gradient overflow with fp16
            full_loss = loss

            if opt.mirror_loss:
                rev_loss = loss_dict['rev_loss'].div(denom)
                mirror_loss = loss_dict['mirror_loss'].div(denom)
                full_loss = full_loss + rev_loss + mirror_loss

            # reconstruction loss
            if opt.reconstruct:
                rec_loss = loss_dict['rec_loss']
                rec_loss = rec_loss.div(denom)
                full_loss = full_loss + rec_loss

            optimizer = self.optim.optimizer

            if self.opt.memory_profiling:
                reporter.report(verbose=True)

                # for obj in gc.get_objects():
                #     try:
                #         if torch.is_tensor(obj) or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                #             # print(varname(obj))
                #             # we can rule out parameter cost later
                #             # if 'parameter' not in type(obj):
                #             # if len(obj.shape) == 3:
                #             # if not isinstance(obj, torch.nn.parameter.Parameter):
                #             #     tensor = obj
                #             #     numel = tensor.
                #             print(type(obj), obj.type(), obj.size())
                #     except:
                #         pass

                # print("Memory profiling complete.")
                # print(torch.cuda.memory_summary())
                # exit()

            if self.cuda:
                with amp.scale_loss(full_loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            if self.opt.memory_profiling:
                print('========= after backward =========')
                reporter.report(verbose=True)

        except RuntimeError as e:
            if 'out of memory' in str(e):
                oom = True
            else:
                raise e

        if oom:
            print("* Warning: out-of-memory in warming up. This is due to the largest batch is too big for the GPU")
        else:
            print("* Warming up successuflly.")

        if self.opt.memory_profiling:
            if hasattr(torch.cuda, 'memory_summary'):
                print(torch.cuda.memory_summary())
            exit()


class XETrainer(BaseTrainer):

    def __init__(self, model, loss_function, train_data, valid_data, dicts, opt, setup_optimizer=True):
        super().__init__(model, loss_function, train_data, valid_data, dicts, opt)

        if self.cuda:
            torch.cuda.set_device(self.opt.gpus[0])
            if self.opt.seed >= 0:
                torch.manual_seed(self.opt.seed)
            self.loss_function = self.loss_function.cuda()
            self.model = self.model.cuda()

        if setup_optimizer:

            self.optim = onmt.Optim(opt)
            self.optim.set_parameters(self.model.parameters())

            if not self.opt.fp16:
                opt_level = "O0"
                keep_batchnorm_fp32 = False
            elif self.opt.fp16_mixed:
                opt_level = "O1"
                keep_batchnorm_fp32 = None
            else:
                opt_level = "O2"
                keep_batchnorm_fp32 = False

            if self.cuda:
                self.model, self.optim.optimizer = amp.initialize(self.model,
                                                                  self.optim.optimizer,
                                                                  opt_level=opt_level,
                                                                  keep_batchnorm_fp32=keep_batchnorm_fp32,
                                                                  loss_scale="dynamic",
                                                                  verbosity=1)
        # An ugly hack to switch between align right and align left
        if hasattr(self.model, 'relative'):
            if self.model.relative:
                self.train_data.src_align_right = True
                self.train_data.tgt_align_right = False
                self.valid_data.src_align_right = True
                self.valid_data.tgt_align_right = False

    def save(self, epoch, valid_ppl, batch_order=None, iteration=-1):

        opt = self.opt
        model = self.model
        dicts = self.dicts

        model_state_dict = self.model.state_dict()
        optim_state_dict = self.optim.state_dict()

        #  drop a checkpoint
        checkpoint = {
            'model': model_state_dict,
            'dicts': dicts,
            'opt': opt,
            'epoch': epoch,
            'iteration': iteration,
            'batch_order': batch_order,
            'optim': optim_state_dict,
            'additional_batch_order': getattr(self, 'additional_batch_order', None),
            'additional_data_iteration': getattr(self, 'additional_data_iteration', None),
            'amp': amp.state_dict()
        }

        file_name = '%s_ppl_%.6f_e%.2f.pt' % (opt.save_model, valid_ppl, epoch)
        print('Writing to %s' % file_name)
        torch.save(checkpoint, file_name)

        # check the save directory here
        checkpoint_dir = os.path.dirname(opt.save_model)
        existed_save_files = checkpoint_paths(checkpoint_dir)
        for save_file in existed_save_files[opt.keep_save_files:]:
            print(" * Deleting old save file %s ...." % save_file)
            os.remove(save_file)

    def eval(self, data):
        total_loss = 0
        total_words = 0
        opt = self.opt

        # batch_order = data.create_order(random=False)
        data_iterator = DataIterator(data, data.collater, data.batches, seed=self.opt.seed,
                                     num_workers=opt.num_workers, epoch=1, buffer_size=opt.buffer_size)
        epoch_iterator = data_iterator.next_epoch_itr(False, pin_memory=False)

        self.model.eval()
        self.loss_function.eval()
        self.model.reset_states()

        if opt.streaming:
            streaming_state = self.model.init_stream()
        else:
            streaming_state = None

        """ PyTorch semantics: save space by not creating gradients """

        data_size = len(epoch_iterator)
        i = 0

        with torch.no_grad():
            # for i in range(len()):
            while not data_iterator.end_of_epoch():
                # batch = data.next()[0]
                batch = next(epoch_iterator)

                batch = rewrap(batch)

                if self.cuda:
                    batch.cuda(fp16=self.opt.fp16 and not self.opt.fp16_mixed)

                """ outputs can be either 
                        hidden states from decoder or
                        prob distribution from decoder generator
                """
                targets = batch.get('target_output')
                tgt_mask = targets.ne(onmt.constants.PAD)
                outputs = self.model(batch, streaming=opt.streaming, target_mask=tgt_mask,
                                     mirror=opt.mirror_loss, streaming_state=streaming_state)

                if opt.streaming:
                    streaming_state = outputs['streaming_state']

                outputs['tgt_mask'] = tgt_mask

                loss_dict = self.loss_function(outputs, targets, model=self.model, eval=True)

                loss_data = loss_dict['data']

                total_loss += loss_data
                total_words += batch.tgt_size
                i = i + 1

        self.model.train()
        self.loss_function.train()
        return total_loss / total_words

    def train_epoch(self, epoch, resume=False, batch_order=None, iteration=0):

        global rec_ppl
        opt = self.opt
        train_data = self.train_data
        streaming = opt.streaming

        self.model.train()
        self.loss_function.train()
        # Clear the gradients of the model
        # self.runner.zero_grad()
        self.model.zero_grad()
        self.model.reset_states()

        # if resume:
        # train_data.batch_order = batch_order
        # train_data.set_index(iteration)
        # print("Resuming from iteration: %d" % iteration)
        # else:
        # batch_order = train_data.create_order()
        # iteration = 0
        dataset = train_data
        data_iterator = DataIterator(dataset, dataset.collater, dataset.batches, seed=self.opt.seed,
                                     num_workers=opt.num_workers, epoch=epoch, buffer_size=opt.buffer_size)
        epoch_iterator = data_iterator.next_epoch_itr(True, pin_memory=opt.pin_memory)

        total_tokens, total_loss, total_words = 0, 0, 0
        total_non_pads = 0
        report_loss, report_tgt_words = 0, 0
        report_src_words = 0
        report_rec_loss, report_rev_loss, report_mirror_loss = 0, 0, 0
        start = time.time()
        n_samples = len(epoch_iterator)

        counter = 0
        num_accumulated_words = 0
        num_accumulated_sents = 0
        denom = 3584
        nan = False

        if opt.streaming:
            streaming_state = self.model.init_stream()
        else:
            streaming_state = None

        i = 0
        # for i in range(iteration, n_samples):
        while not data_iterator.end_of_epoch():

            curriculum = (epoch < opt.curriculum)

            # batches = [train_data.next(curriculum=curriculum)[0]]

            if (len(self.additional_data) > 0 and
                    i % self.additional_data_ratio[0] == 0):
                for j in range(len(self.additional_data)):
                    for k in range(self.additional_data_ratio[j + 1]):
                        if self.additional_data_iteration[j] == len(self.additional_data[j]):
                            self.additional_data_iteration[j] = 0
                            self.additional_data[j].shuffle()
                            self.additional_batch_order[j] = self.additional_data[j].create_order()

                        batches.append(self.additional_data[j].next()[0])
                        self.additional_data_iteration[j] += 1

            # for b in range(len(batches)):
            batch = next(epoch_iterator)

            batch = rewrap(batch)

            if self.cuda:
                batch.cuda(fp16=self.opt.fp16 and not self.opt.fp16_mixed)

            # if opt.streaming:
            #     if train_data.is_new_stream():
            #         streaming_state = self.model.init_stream()
            # else:
            #     streaming_state = None

            oom = False
            try:
                # outputs is a dictionary containing keys/values necessary for loss function
                # can be flexibly controlled within models for easier extensibility
                targets = batch.get('target_output')
                tgt_mask = targets.data.ne(onmt.constants.PAD)
                outputs = self.model(batch, streaming=opt.streaming, target_mask=tgt_mask,
                                     zero_encoder=opt.zero_encoder,
                                     mirror=opt.mirror_loss, streaming_state=streaming_state)

                batch_size = batch.size

                outputs['tgt_mask'] = tgt_mask

                loss_dict = self.loss_function(outputs, targets, model=self.model)
                loss_data = loss_dict['data']
                loss = loss_dict['loss'].div(denom)  # a little trick to avoid gradient overflow with fp16
                full_loss = loss

                if opt.mirror_loss:
                    rev_loss = loss_dict['rev_loss'].div(denom)
                    rev_loss_data = loss_dict['rev_loss_data']
                    mirror_loss = loss_dict['mirror_loss'].div(denom)
                    full_loss = full_loss + rev_loss + mirror_loss
                    mirror_loss_data = loss_dict['mirror_loss'].item()
                else:
                    rev_loss = None
                    rev_loss_data = None
                    mirror_loss_data = 0

                # reconstruction loss
                if opt.reconstruct:
                    rec_loss = loss_dict['rec_loss']
                    rec_loss = rec_loss.div(denom)
                    full_loss = full_loss + rec_loss
                    rec_loss_data = loss_dict['rec_loss_data']
                    # print(rec_loss_data)
                else:
                    # full_loss = loss
                    rec_loss_data = None

                optimizer = self.optim.optimizer

                if self.cuda:
                    with amp.scale_loss(full_loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                else:
                    loss.backward()

            except RuntimeError as e:
                if 'out of memory' in str(e):
                    print('| WARNING: ran out of memory on GPU , skipping batch')
                    oom = True
                    torch.cuda.empty_cache()
                    loss = 0
                    if opt.streaming:  # reset stream in this case ...
                        streaming_state = self.model.init_stream()
                else:
                    raise e

            if loss != loss:
                # catching NAN problem
                oom = True
                self.model.zero_grad()
                self.optim.zero_grad()
                num_accumulated_words = 0
                num_accumulated_sents = 0
                print("Warning!!! Loss is Nan")

            if not oom:
                src_size = batch.src_size
                tgt_size = batch.tgt_size

                counter = counter + 1
                num_accumulated_words += tgt_size
                num_accumulated_sents += batch_size

                #   We only update the parameters after getting gradients from n mini-batches
                update_flag = False
                if 0 < opt.batch_size_update <= num_accumulated_words:
                    update_flag = True
                elif counter >= opt.update_frequency and 0 >= opt.batch_size_update:
                    update_flag = True
                elif i == n_samples - 1:  # update for the last minibatch
                    update_flag = True

                if update_flag:
                    grad_denom = 1 / denom
                    if self.opt.normalize_gradient:
                        grad_denom = num_accumulated_words / denom
                    normalize_gradients(amp.master_params(optimizer), grad_denom)
                    # Update the parameters.
                    if self.opt.max_grad_norm > 0:
                        torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), self.opt.max_grad_norm)
                    self.optim.step(grad_denom=grad_denom)
                    self.optim.zero_grad()
                    self.model.zero_grad()
                    counter = 0
                    num_accumulated_words = 0
                    num_accumulated_sents = 0
                    num_updates = self.optim._step
                    if opt.save_every > 0 and num_updates % opt.save_every == -1 % opt.save_every:
                        valid_loss = self.eval(self.valid_data)
                        valid_ppl = math.exp(min(valid_loss, 100))
                        print('Validation perplexity: %g' % valid_ppl)

                        ep = float(epoch) - 1. + ((float(i) + 1.) / n_samples)

                        self.save(ep, valid_ppl, batch_order=batch_order, iteration=i)

                num_words = tgt_size
                report_loss += loss_data
                report_tgt_words += num_words
                report_src_words += src_size
                total_loss += loss_data
                total_words += num_words
                total_tokens += batch.get('target_output').nelement()
                total_non_pads += batch.get('target_output').ne(onmt.constants.PAD).sum().item()
                optim = self.optim
                batch_efficiency = total_non_pads / total_tokens

                if opt.reconstruct:
                    report_rec_loss += rec_loss_data

                if opt.mirror_loss:
                    report_rev_loss += rev_loss_data
                    report_mirror_loss += mirror_loss_data

                if i == 0 or (i % opt.log_interval == -1 % opt.log_interval):
                    log_string = ("Epoch %2d, %5d/%5d; ; ppl: %6.2f ; " %
                                  (epoch, i + 1, len(data_iterator),
                                   math.exp(report_loss / report_tgt_words)))

                    if opt.reconstruct:
                        rec_ppl = math.exp(report_rec_loss / report_src_words.item())
                        log_string += (" rec_ppl: %6.2f ; " % rec_ppl)

                    if opt.mirror_loss:
                        rev_ppl = math.exp(report_rev_loss / report_tgt_words)
                        log_string += (" rev_ppl: %6.2f ; " % rev_ppl)
                        # mirror loss per word
                        log_string += (" mir_loss: %6.2f ; " % (report_mirror_loss / report_tgt_words))

                    log_string += ("lr: %.7f ; updates: %7d; " %
                                   (optim.getLearningRate(),
                                    optim._step))

                    log_string += ("%5.0f src tok/s; %5.0f tgt tok/s; " %
                                   (report_src_words / (time.time() - start),
                                    report_tgt_words / (time.time() - start)))

                    log_string += ("%s elapsed" %
                                   str(datetime.timedelta(seconds=int(time.time() - self.start_time))))

                    print(log_string)

                    report_loss = 0
                    report_tgt_words, report_src_words = 0, 0
                    report_rec_loss, report_rev_loss, report_mirror_loss = 0, 0, 0
                    start = time.time()

                i = i + 1

        return total_loss / total_words

    # def run(self, save_file=None):
    def run(self, checkpoint=None):

        opt = self.opt
        model = self.model
        optim = self.optim

        # Try to load the save_file
        # checkpoint = None
        # if save_file:
        #     checkpoint = torch.load(save_file, map_location=lambda storage, loc: storage)

        if checkpoint is not None:
            self.model.load_state_dict(checkpoint['model'])
            prec_opt = checkpoint['opt'] if 'opt' in checkpoint else None

            if not opt.reset_optim:
                self.optim.load_state_dict(checkpoint['optim'])
                if prec_opt is not None and hasattr(prec_opt, "fp16_mixed"):
                    # Only load amp information if the mode is the same
                    # Maybe its better to change between optimization mode?
                    if opt.fp16_mixed == prec_opt.fp16_mixed and opt.fp16 == prec_opt.fp16:
                        if 'amp' in checkpoint:
                            amp.load_state_dict(checkpoint['amp'])

                if 'batch_order' in checkpoint:
                    batch_order = checkpoint['batch_order']
                    iteration = checkpoint['iteration'] + 1
                else:
                    batch_order = None
                    iteration = 0
                opt.start_epoch = int(math.floor(float(checkpoint['epoch'] + 1)))

                resume = True
                if len(self.additional_data) > 0:
                    if 'additional_batch_order' in checkpoint:
                        self.additional_batch_order = checkpoint['additional_batch_order']
                        self.additional_data_iteration = checkpoint['additional_data_iteration']
                    else:
                        self.init_additional_data()
            else:
                batch_order = None
                iteration = 0
                resume = False
                self.init_additional_data()

            del checkpoint['model']
            del checkpoint['optim']
            del checkpoint
        else:
            batch_order = None
            iteration = 0
            print('Initializing model parameters')
            init_model_parameters(model, opt)
            resume = False
            self.init_additional_data()

        if opt.load_encoder_from:
            self.load_encoder_weight(opt.load_encoder_from)

        if opt.load_decoder_from:
            self.load_decoder_weight(opt.load_decoder_from)

        # if we are on a GPU: warm up the memory allocator
        if self.cuda:
            self.warm_up()

        valid_loss = self.eval(self.valid_data)
        valid_ppl = math.exp(min(valid_loss, 100))
        print('Validation perplexity: %g' % valid_ppl)

        self.start_time = time.time()

        for epoch in range(opt.start_epoch, opt.start_epoch + opt.epochs):
            print('')

            #  (1) train for one epoch on the training set
            train_loss = self.train_epoch(epoch, resume=resume,
                                          batch_order=batch_order,
                                          iteration=iteration)
            train_ppl = math.exp(min(train_loss, 100))
            print('Train perplexity: %g' % train_ppl)

            #  (2) evaluate on the validation set
            valid_loss = self.eval(self.valid_data)
            valid_ppl = math.exp(min(valid_loss, 100))
            print('Validation perplexity: %g' % valid_ppl)

            self.save(epoch, valid_ppl)
            batch_order = None
            iteration = None
            resume = False

    def init_additional_data(self):
        self.additional_batch_order = []
        self.additional_data_iteration = []
        for i in range(len(self.additional_data)):
            self.additional_data_iteration.append(0)
            self.additional_data[i].shuffle()
            self.additional_batch_order.append(self.additional_data[i].create_order())
