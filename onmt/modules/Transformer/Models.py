import numpy as np
import torch, math
import torch.nn as nn
from torch.autograd import Variable
from onmt.modules.Transformer.Layers import EncoderLayer, DecoderLayer, PositionalEncoding, variational_dropout, PrePostProcessing
from onmt.modules.BaseModel import NMTModel, Reconstructor
import onmt
from onmt.modules.WordDrop import embedded_dropout
from onmt.modules.Checkpoint import checkpoint

def custom_encoder_layer(module):
    def custom_forward(*args):
        output = module(*args)
        return output
    return custom_forward
def custom_decoder_layer(module):
    def custom_forward(*args):
        output = module(*args)
        return output
    return custom_forward
    
def detach(variable, requires_grad=True):
    
    return Variable(variable.data, requires_grad=requires_grad)

class TransformerEncoder(nn.Module):
    """Encoder in 'Attention is all you need'
    
    Args:
        opt: list of options ( see train.py )
        dicts : dictionary (for source language)
        
    """
    
    def __init__(self, opt, dicts, positional_encoder):
    
        super(TransformerEncoder, self).__init__()
        
        self.model_size = opt.model_size
        self.n_heads = opt.n_heads
        self.inner_size = opt.inner_size
        self.layers = opt.layers
        self.dropout = opt.dropout
        self.word_dropout = opt.word_dropout
        self.attn_dropout = opt.attn_dropout
        self.emb_dropout = opt.emb_dropout
        self.time = opt.time
        self.version = opt.version
        
        self.word_lut = nn.Embedding(dicts.size(),
                                     self.model_size,
                                     padding_idx=onmt.Constants.PAD)
        
        if opt.time == 'positional_encoding':
            self.time_transformer = positional_encoder
        elif opt.time == 'gru':
            self.time_transformer = nn.GRU(self.model_size, self.model_size, 1, batch_first=True)
        elif opt.time == 'lstm':
            self.time_transformer = nn.LSTM(self.model_size, self.model_size, 1, batch_first=True)
        
        self.preprocess_layer = PrePostProcessing(self.model_size, self.emb_dropout, sequence='d', static=False)
        
        self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
        
        self.positional_encoder = positional_encoder
        
        self.layer_modules = nn.ModuleList([EncoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout, version=self.version) for _ in range(self.layers)])
        
        self.checkpointed_outputs = list()
        
        self.checkpointed_inputs = list()

    def forward(self, input, checkpoint=0):
        """
        Inputs Shapes: 
            input: batch_size x len_src (wanna tranpose)
        
        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src 
            
        """
        
        """ Embedding: batch_size x len_src x d_model """
        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        """ Scale the emb by sqrt(d_model) """
        
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        emb = self.time_transformer(emb)
        if isinstance(emb, tuple):
            emb = emb[0]
        emb = self.preprocess_layer(emb)
        
        mask_src = input.data.eq(onmt.Constants.PAD).unsqueeze(1) # batch_size x len_src x 1 for broadcasting
        
        pad_mask = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        #~ pad_mask = None
        
        context = emb.contiguous()
        
        for i, layer in enumerate(self.layer_modules):
            #~ if i < onmt.Constants.checkpointing:
                #~ context = checkpoint(custom_encoder_layer(layer), context, mask_src, pad_mask)
            #~ else:
                context = layer(context, mask_src, pad_mask)      # batch_size x len_src x d_model
        
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        if self.version == 1.0:
            context = self.postprocess_layer(context)
            
        self.checkpointed_outputs.append(context)
        
        return context
    
    def backward(self, grad_context):
        
        context = self.checkpointed_outputs[-1]
        
        context.backward(grad_context)
        
        self.checkpointed_outputs.clear()

class TransformerDecoder(nn.Module):
    """Encoder in 'Attention is all you need'
    
    Args:
        opt
        dicts 
        
        
    """
    
    def __init__(self, opt, dicts, positional_encoder):
    
        super(TransformerDecoder, self).__init__()
        
        self.model_size = opt.model_size
        self.n_heads = opt.n_heads
        self.inner_size = opt.inner_size
        self.layers = opt.layers
        self.dropout = opt.dropout
        self.word_dropout = opt.word_dropout 
        self.attn_dropout = opt.attn_dropout
        self.emb_dropout = opt.emb_dropout
        self.time = opt.time
        self.version = opt.version
        
        if opt.time == 'positional_encoding':
            self.time_transformer = positional_encoder
        elif opt.time == 'gru':
            self.time_transformer = nn.GRU(self.model_size, self.model_size, 1, batch_first=True)
        elif opt.time == 'lstm':
            self.time_transformer = nn.LSTM(self.model_size, self.model_size, 1, batch_first=True)
        
        self.preprocess_layer = PrePostProcessing(self.model_size, self.emb_dropout, sequence='d')
        if self.version == 1.0:
            self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')
        
        self.word_lut = nn.Embedding(dicts.size(),
                                     self.model_size,
                                     padding_idx=onmt.Constants.PAD)
        
        self.positional_encoder = positional_encoder
        
        self.layer_modules = nn.ModuleList([DecoderLayer(self.n_heads, self.model_size, self.dropout, self.inner_size, self.attn_dropout, version=self.version) for _ in range(self.layers)])
        
        len_max = self.positional_encoder.len_max
        mask = torch.ByteTensor(np.triu(np.ones((len_max,len_max)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)
        
        self.checkpointed_outputs = list()
        
        self.checkpointed_inputs = list()
    
    def renew_buffer(self, new_len):
        
        self.positional_encoder.renew(new_len)
        mask = torch.ByteTensor(np.triu(np.ones((new_len,new_len)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)
        
    def forward(self, input, context, src, checkpoint=0):
        """
        Inputs Shapes: 
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x len_src x d_model
            mask_src (Tensor) batch_size x len_src
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x len_src
            
        """
        
        """ Embedding: batch_size x len_tgt x d_model """
        emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        emb = self.time_transformer(emb)
        if isinstance(emb, tuple):
            emb = emb[0]
        emb = self.preprocess_layer(emb)
        

        mask_src = src.data.eq(onmt.Constants.PAD).unsqueeze(1)
        
        pad_mask_src = torch.autograd.Variable(src.data.ne(onmt.Constants.PAD))
        
        len_tgt = input.size(1)
        mask_tgt = input.data.eq(onmt.Constants.PAD).unsqueeze(1) + self.mask[:len_tgt, :len_tgt]
        mask_tgt = torch.gt(mask_tgt, 0)
        
        output = emb.contiguous()
        
        pad_mask_tgt = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        pad_mask_src = torch.autograd.Variable(1 - mask_src.squeeze(1))
        
        
        for i, layer in enumerate(self.layer_modules):
            
            #~ if i < onmt.Constants.checkpointing:
                #~ output, coverage = checkpoint(custom_decoder_layer(layer), output, context, mask_tgt, mask_src, 
                                            #~ pad_mask_tgt, pad_mask_src)
            #~ else:
                output, coverage = layer(output, context, mask_tgt, mask_src, 
                                            pad_mask_tgt, pad_mask_src) # batch_size x len_src x d_model
        
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        if self.version == 1.0:
            output = self.postprocess_layer(output)
            
        self.checkpointed_outputs.append(output)
        
        return output, coverage
        
    def backward(self, grad_output):
    
        output = self.checkpointed_outputs[-1]
        
        output.backward(grad_output)
        
        self.checkpointed_outputs.clear()
    
    def step(self, input, context, src, buffer=None):
        """
        Inputs Shapes: 
            input: (Variable) batch_size x len_tgt (wanna tranpose)
            context: (Variable) batch_size x len_src x d_model
            mask_src (Tensor) batch_size x len_src
            buffer (List of tensors) List of batch_size * len_tgt-1 * d_model for self-attention recomputing
        Outputs Shapes:
            out: batch_size x len_tgt x d_model
            coverage: batch_size x len_tgt x len_src
            
        """
        
            
        output_buffer = list()
        
        batch_size = input.size(0)
        
        input_ = input[:,-1].unsqueeze(1)
        """ Embedding: batch_size x 1 x d_model """
        emb = self.word_lut(input_)
     
        if self.time == 'positional_encoding':
            emb = emb * math.sqrt(self.model_size)
        """ Adding positional encoding """
        if self.time == 'positional_encoding':
            emb = self.time_transformer(emb, t=input.size(1))
        else:
            prev_h = buffer[0] if buffer is None else None
            emb = self.time_transformer(emb, prev_h)
            # output_buffer.append(emb[1])
            buffer[0] = emb[1]
            
        if isinstance(emb, tuple):
            emb = emb[0] # emb should be batch_size x 1 x dim
        
            
        # Preprocess layer: adding dropout
        emb = self.preprocess_layer(emb)
        
        # batch_size x 1 x len_src
        mask_src = src.data.eq(onmt.Constants.PAD).unsqueeze(1)
        
        pad_mask_src = torch.autograd.Variable(src.data.ne(onmt.Constants.PAD))
        
        len_tgt = input.size(1)
        mask_tgt = input.data.eq(onmt.Constants.PAD).unsqueeze(1) + self.mask[:len_tgt, :len_tgt]
        mask_tgt = torch.gt(mask_tgt, 0)
        mask_tgt = mask_tgt[:, -1, :].unsqueeze(1)
                
        output = emb.contiguous()
        
        pad_mask_tgt = torch.autograd.Variable(input.data.ne(onmt.Constants.PAD)) # batch_size x len_src
        pad_mask_src = torch.autograd.Variable(1 - mask_src.squeeze(1))
        
        
        for i, layer in enumerate(self.layer_modules):
            
            buffer_ = buffer[i] if buffer is not None else None
            assert(output.size(1) == 1)
            output, coverage, buffer_ = layer.step(output, context, mask_tgt, mask_src, 
                                        pad_mask_tgt=None, pad_mask_src=None, buffer=buffer_) # batch_size x len_src x d_model
            
            output_buffer.append(buffer_)
        
        buffer = torch.stack(output_buffer)
        # From Google T2T
        # if normalization is done in layer_preprocess, then it should also be done
        # on the output, since the output can grow very large, being the sum of
        # a whole stack of unnormalized layer outputs.    
        if self.version == 1.0:
            output = self.postprocess_layer(output)
        
        return output, coverage, buffer
    
  
        
class Transformer(NMTModel):
    """Main model in 'Attention is all you need' """
    
        
    def forward(self, input, checkpoint=0): 
        """
        Inputs Shapes: 
            src: len_src x batch_size
            tgt: len_tgt x batch_size
        
        Outputs Shapes:
            out:      batch_size*len_tgt x model_size
            
            
        """
        src = input[0]
        tgt = input[1][:-1]  # exclude last target from inputs
        
        src = src.transpose(0, 1) # transpose to have batch first
        tgt = tgt.transpose(0, 1)
        
        context = self.encoder(src, checkpoint=checkpoint)
        
        context = Variable(context.data, requires_grad=True)
        self.saved_for_backward['context'] = context
        
        output, coverage = self.decoder(tgt, context, src, checkpoint=checkpoint)
        
        
        self.saved_for_backward['decoder_coverage'] = coverage
        
        output = detach(output, requires_grad=self.training)
        self.saved_for_backward['decoder_output'] = output
        
        output = output.transpose(0, 1) # transpose to have time first, like RNN models
        
        return output
        
    def backward(self, output, grad_output):
        
        output.backward(grad_output)
        
        grad_output = self.saved_for_backward['decoder_output'].grad.data
        self.decoder.backward(grad_output)
        
        grad_context = self.saved_for_backward['context'].grad.data
        self.encoder.backward(grad_context)
        
        self.saved_for_backward.clear()
    

#~ class TrasnformerReconstructor(Reconstructor):
    #~ 
    #~ def forward(self, src, contexts, context_mask):
        #~ 
        #~ """
        #~ Inputs Shapes: 
            #~ src: len_src x batch_size
            #~ context: batch_size x len_tgt x model_size
            #~ context_mask: batch_size x len_tgt
        #~ 
        #~ Outputs Shapes:
            #~ output:      batch_size*(len_src-1) x model_size
            #~ 
            #~ 
        #~ """
        #~ src_input = src[:-1] # exclude last unit from source
        #~ 
        #~ src_input = src_input.transpose(0, 1) # transpose to have batch first
        #~ output, coverage = self.decoder(src, context, context_mask)
        #~ 
        #~ output = output.transpose(0, 1) # transpose to have time first, like RNN models
        #~ 
        #~ return output
        #~ source = source.transpose(0, 1)
        
        
