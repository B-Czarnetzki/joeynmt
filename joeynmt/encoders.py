# coding: utf-8

"""
Various encoders
"""

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

from joeynmt.helpers import freeze_params

#pylint: disable=abstract-method


class Encoder(nn.Module):
    """
    Base encoder class
    """
    @property
    def output_size(self):
        """
        Return the output size

        :return:
        """
        return self._output_size


class RecurrentEncoder(Encoder):
    """Encodes a sequence of word embeddings"""

    #pylint: disable=unused-argument
    def __init__(self,
                 rnn_type: str = "gru",
                 hidden_size: int = 1,
                 emb_size: int = 1,
                 num_layers: int = 1,
                 dropout: float = 0.,
                 bidirectional: bool = True,
                 freeze: bool = False,
                 **kwargs) -> None:
        """
        Create a new recurrent encoder.

        :param rnn_type:
        :param hidden_size:
        :param emb_size:
        :param num_layers:
        :param dropout:
        :param bidirectional:
        :param freeze: freeze the parameters of the encoder during training
        :param kwargs:
        """

        super(RecurrentEncoder, self).__init__()

        self.rnn_input_dropout = torch.nn.Dropout(p=dropout, inplace=False)
        self.type = rnn_type
        self.emb_size = emb_size

        rnn = nn.GRU if rnn_type == "gru" else nn.LSTM

        self.rnn = rnn(
            emb_size, hidden_size, num_layers, batch_first=True,
            bidirectional=bidirectional,
            dropout=dropout if num_layers > 1 else 0.)

        self._output_size = 2 * hidden_size if bidirectional else hidden_size

        if freeze:
            freeze_params(self)

    # pylint: disable=invalid-name, unused-argument
    def _check_shapes_input_forward(self, embed_src: Tensor, src_length: Tensor,
                                    mask: Tensor) -> None:
        """
        Make sure the shape of the inputs to `self.forward` are correct.
        Same input semantics as `self.forward`.

        :param embed_src: embedded source tokens
        :param src_length: source length
        :param mask: source mask
        """
        assert embed_src.shape[0] == src_length.shape[0]
        assert embed_src.shape[2] == self.emb_size
        assert len(src_length.shape) == 1

    #pylint: disable=arguments-differ
    def forward(self, embed_src: Tensor, src_length: Tensor, mask: Tensor) \
            -> (Tensor, Tensor):
        """
        Applies a bidirectional RNN to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param mask: indicates padding areas (zeros where padding), shape
            (batch_size, src_len, embed_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        self._check_shapes_input_forward(embed_src=embed_src,
                                         src_length=src_length,
                                         mask=mask)
        # apply dropout ot the rnn input
        embed_src = self.rnn_input_dropout(embed_src)

        packed = pack_padded_sequence(embed_src, src_length, batch_first=True)
        output, hidden = self.rnn(packed)

        #pylint: disable=unused-variable
        if isinstance(hidden, tuple):
            hidden, memory_cell = hidden

        output, _ = pad_packed_sequence(output, batch_first=True)
        # hidden: dir*layers x batch x hidden
        # output: batch x max_length x directions*hidden
        batch_size = hidden.size()[1]
        # separate final hidden states by layer and direction
        hidden_layerwise = hidden.view(self.rnn.num_layers,
                                       2 if self.rnn.bidirectional else 1,
                                       batch_size, self.rnn.hidden_size)
        # final_layers: layers x directions x batch x hidden

        # concatenate the final states of the last layer for each directions
        # thanks to pack_padded_sequence final states don't include padding
        fwd_hidden_last = hidden_layerwise[-1:, 0]
        bwd_hidden_last = hidden_layerwise[-1:, 1]

        # only feed the final state of the top-most layer to the decoder
        #pylint: disable=no-member
        hidden_concat = torch.cat(
            [fwd_hidden_last, bwd_hidden_last], dim=2).squeeze(0)
        # final: batch x directions*hidden
        return output, hidden_concat

    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.rnn)


class SpeechRecurrentEncoder(Encoder):
    """Encodes a sequence of word embeddings"""

    #pylint: disable=unused-argument
    def __init__(self,
                 rnn_type: str = "gru",
                 hidden_size: int = 1,
                 linear_hidden_size_1: int = 1,
                 linear_hidden_size_2: int = 1,
                 emb_size: int = 1,
                 num_layers: int = 1,
                 dropout: float = 0.,
                 bidirectional: bool = True,
                 freeze: bool = False,
                 activation: str = "relu",
                 last_activation: str = "None",
                 layer_norm: bool = False,
                 emb_norm: bool = False,
                 same_weights: bool = False,
                 **kwargs) -> None:
        """
        Create a new recurrent encoder.

        :param rnn_type:
        :param hidden_size:
        :param emb_size:
        :param num_layers:
        :param dropout:
        :param bidirectional:
        :param freeze: freeze the parameters of the encoder during training
        :param kwargs:
        """

        super(SpeechRecurrentEncoder, self).__init__()

        self.rnn_input_dropout = torch.nn.Dropout(p=dropout, inplace=False)
        self.type = rnn_type
        self.emb_size = emb_size
        self.lila1 = nn.Linear(emb_size, linear_hidden_size_1)
        self.lila2 = nn.Linear(linear_hidden_size_1, linear_hidden_size_2)
        self.activation = activation
        self.last_activation = last_activation
        self.conv1 = nn.Sequential(
            nn.Conv2d(1, 16,
                      kernel_size=3, stride=2, padding=1))
        self.conv2 = nn.Sequential(
            nn.Conv2d(16, 16,
                      kernel_size=3, stride=2, padding=1))
        self.layer_norm = layer_norm
        self.emb_norm = emb_norm
        if self.layer_norm:
            self.norm1 = nn.LayerNorm(hidden_size)
            self.norm2 = nn.LayerNorm(hidden_size)
            self.norm_out = nn.LayerNorm(
                2 * hidden_size if bidirectional else hidden_size)
        if self.emb_norm:
            self.norm_emb = nn.LayerNorm(emb_size)

        rnn = nn.GRU if rnn_type == "gru" else nn.LSTM

        self.rnn = rnn(
            4 * linear_hidden_size_2, hidden_size, num_layers, batch_first=True,
            bidirectional=bidirectional,
            dropout=dropout if num_layers > 1 else 0.)

        self._output_size = 2 * hidden_size if bidirectional else hidden_size

        if freeze:
            freeze_params(self)

    # pylint: disable=invalid-name, unused-argument
    def _check_shapes_input_forward(self, embed_src: Tensor, src_length: Tensor) \
            -> None:
        """
        Make sure the shape of the inputs to `self.forward` are correct.
        Same input semantics as `self.forward`.

        :param embed_src: embedded source tokens
        :param src_length: source length
        """
        assert embed_src.shape[0] == src_length.shape[0]
        assert embed_src.shape[2] == self.emb_size
        assert len(src_length.shape) == 1

    #pylint: disable=arguments-differ
    def forward(self, embed_src: Tensor, src_length: Tensor, mask: Tensor,
                conv_length: Tensor) -> (Tensor, Tensor):
        """
        Applies a bidirectional RNN to sequence of embeddings x.
        The input mini-batch x needs to be sorted by src length.
        x and mask should have the same dimensions [batch, time, dim].

        :param embed_src: embedded src inputs,
            shape (batch_size, src_len, embed_size)
        :param src_length: length of src inputs
            (counting tokens before padding), shape (batch_size)
        :param conv_length: length of src inputs after convolutions
            (counting tokens before padding), shape (batch_size)
        :return:
            - output: hidden states with
                shape (batch_size, max_length, directions*hidden),
            - hidden_concat: last hidden state with
                shape (batch_size, directions*hidden)
        """
        self._check_shapes_input_forward(embed_src=embed_src,
                                         src_length=src_length)

        # embeddings normalization
        if self.emb_norm:
            embed_src = self.norm_emb(embed_src)

        # 2 layers with nonlinear activation
        if self.activation == "tanh":
            lila_out1 = torch.tanh(self.lila1(embed_src))
            lila_out2 = torch.tanh(self.lila2(lila_out1))
        else:
            lila_out1 = torch.relu(self.lila1(embed_src))
            lila_out2 = torch.relu(self.lila2(lila_out1))

        lila_out2 = lila_out2.unsqueeze(1)
        #print("\nlila1 output shape: ", lila_out1.size())
        #print("Convolution input shape: ", lila_out2.size())
        # 2 convolutional layers
        conv_out1 = self.conv1(lila_out2)
        #print("convolution 1 output shape: ", conv_out1.size())

        # layer normalization
        if self.layer_norm:
            conv_out1 = self.norm1(conv_out1)

        conv_out2 = self.conv2(conv_out1)
        #print("Convolution 2 output shape: ", conv_out2.size())
        conv_out2 = conv_out2.transpose(1, 3).transpose(1, 2)
        #print("Convolution 2 output tranposed: ", conv_out2.size())

        conv_out2 = conv_out2.flatten(start_dim=2)

        # layer normalization
        if self.layer_norm:
            conv_out2 = self.norm2(conv_out2)

        #print("convolution 2 output flattend: ", conv_out2.size())
        # apply dropout to the rnn input
        conv_do = self.rnn_input_dropout(conv_out2)

        packed = pack_padded_sequence(conv_do, conv_length, batch_first=True)
        output, hidden = self.rnn(packed)

        #pylint: disable=unused-variable
        if isinstance(hidden, tuple):
            hidden, memory_cell = hidden

        output, _ = pad_packed_sequence(output, batch_first=True)
        # hidden: dir*layers x batch x hidden
        # output: batch x max_length x directions*hidden
        batch_size = hidden.size()[1]
        # separate final hidden states by layer and direction
        hidden_layerwise = hidden.view(self.rnn.num_layers,
                                       2 if self.rnn.bidirectional else 1,
                                       batch_size, self.rnn.hidden_size)
        # final_layers: layers x directions x batch x hidden

        # concatenate the final states of the last layer for each directions
        # thanks to pack_padded_sequence final states don't include padding
        fwd_hidden_last = hidden_layerwise[-1:, 0]
        bwd_hidden_last = hidden_layerwise[-1:, 1]

        # only feed the final state of the top-most layer to the decoder
        #pylint: disable=no-member
        hidden_concat = torch.cat(
            [fwd_hidden_last, bwd_hidden_last], dim=2).squeeze(0)

        # layer normalization
        if self.layer_norm:
            output = self.norm_out(output)

        # a non-linear activation for the output layer
        if self.last_activation == "relu":
            output = torch.relu(output)
        elif self.last_activation == "tanh":
            output = torch.tanh(output)

        # final: batch x directions*hidden
        return output, hidden_concat

    @property
    def __repr__(self):
        return "%s(%r)" % (self.__class__.__name__, self.rnn)
