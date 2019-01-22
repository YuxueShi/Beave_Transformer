# -*- coding: utf-8 -*-

import torch
import torch.nn as nn


class FeedForward(nn.Module):
    def __init__(self, hidden_size, inner_size, dropout=0.0):
        super(FeedForward, self).__init__()
        self.linear_in = nn.Linear(hidden_size, inner_size)
        self.linear_out = nn.Linear(inner_size, hidden_size)
        self.dropout = nn.Dropout(dropout)
        self.relu = nn.ReLU()

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.linear_in.weight)
        nn.init.xavier_uniform_(self.linear_out.weight)
        nn.init.constant_(self.linear_in.bias, 0.)
        nn.init.constant_(self.linear_out.bias, 0.)

    def forward(self, x):
        y = self.linear_in(x)
        y = self.relu(y)
        y = self.dropout(y)
        y = self.linear_out(y)
        return y


class EncoderLayer(nn.Module):

    def __init__(self, hidden_size, dropout, head_count, ff_size):
        super(EncoderLayer, self).__init__()

        self.self_attn = MultiHeadedAttention(head_count, hidden_size, dropout=dropout)
        self.feed_forward = FeedForward(hidden_size, ff_size, dropout)
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.ModuleList([nn.LayerNorm(hidden_size) for _ in range(2)])

    def forward(self, x, mask):
        # self attention
        y = self.self_attn(x, memory=None, mask=mask)
        x = self.norm[0](x + self.dropout(y))

        # feed forward
        y = self.feed_forward(x)
        x = self.norm[1](x + self.dropout(y))
        return x


class Encoder(nn.Module):
    def __init__(self, num_layers, num_heads, hidden_size, dropout, ff_size, embedding):
        super(Encoder, self).__init__()
        self.embedding = embedding
        self.layers = nn.ModuleList([EncoderLayer(hidden_size, dropout, num_heads, ff_size) for _ in range(num_layers)])

    def forward(self, src, src_pad):
        src_mask = src_pad.unsqueeze(1).repeat(1, src.size(1), 1)
        output = self.embedding(src)
        for layer in self.layers:
            output = layer(output, src_mask)
        return output


class DecoderLayer(nn.Module):

    def __init__(self, hidden_size, dropout, head_count, ff_size):
        super(DecoderLayer, self).__init__()
        self.self_attn = MultiHeadedAttention(head_count, hidden_size, dropout=dropout)
        self.src_attn = MultiHeadedAttention(head_count, hidden_size, dropout=dropout)
        self.feed_forward = FeedForward(hidden_size, ff_size, dropout)
        self.norm = nn.ModuleList([nn.LayerNorm(hidden_size) for _ in range(3)])
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, enc_out, src_mask, tgt_mask, previous=None):
        all_input = x if previous is None else torch.cat((previous, x), dim=1)

        # self attention
        y = self.self_attn(x, memory=all_input, mask=tgt_mask)
        x = self.norm[0](x + self.dropout(y))

        # encoder decoder attention
        y = self.src_attn(x, memory=enc_out, mask=src_mask)
        x = self.norm[1](x + self.dropout(y))

        # feed forward
        y = self.feed_forward(x)
        x = self.norm[2](x + self.dropout(y))
        return x, all_input


class Decoder(nn.Module):

    def __init__(self, num_layers, num_heads, hidden_size, dropout, ff_size, embedding):
        super(Decoder, self).__init__()
        self.embedding = embedding
        self.layers = nn.ModuleList([DecoderLayer(hidden_size, dropout, num_heads, ff_size) for _ in range(num_layers)])
        self.register_buffer("upper_triangle", torch.triu(torch.ones(1000, 1000), diagonal=1).byte())

    def forward(self, tgt, enc_out, src_pad, tgt_pad, previous=None, timestep=0):

        output = self.embedding(tgt, timestep)
        tgt_len = tgt.size(1)

        src_mask = src_pad.unsqueeze(1).repeat(1, tgt_len, 1)
        tgt_mask = tgt_pad.unsqueeze(1).repeat(1, tgt_len, 1)
        upper_triangle = self.upper_triangle[:tgt_len, :tgt_len]
        # tgt mask: 0 if not upper and not pad, 1 or 2 otherwise

        tgt_mask = torch.gt(tgt_mask + upper_triangle, 0)
        saved_inputs = []
        for i, layer in enumerate(self.layers):
            prev_layer = None if previous is None else previous[:, i]
            tgt_mask = tgt_mask if previous is None else None

            output, all_input = layer(output, enc_out, src_mask, tgt_mask, prev_layer)
            saved_inputs.append(all_input)
        return output, torch.stack(saved_inputs, dim=1)


class MultiHeadedAttention(nn.Module):

    def __init__(self, head_count, model_dim, dropout=0.0):
        self.dim_per_head = model_dim // head_count
        self.head_count = head_count

        super(MultiHeadedAttention, self).__init__()

        self.linear_q = nn.Linear(model_dim, model_dim)
        self.linear_kv = nn.Linear(model_dim, model_dim * 2)
        self.linear_qkv = nn.Linear(model_dim, model_dim * 3)

        self.softmax = nn.Softmax(dim=-1)
        self.dropout = nn.Dropout(dropout)
        self.final_linear = nn.Linear(model_dim, model_dim)

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.linear_q.weight)
        nn.init.xavier_uniform_(self.linear_kv.weight)
        nn.init.xavier_uniform_(self.linear_qkv.weight)
        nn.init.xavier_uniform_(self.final_linear.weight)
        nn.init.constant_(self.linear_q.bias, 0.)
        nn.init.constant_(self.linear_kv.bias, 0.)
        nn.init.constant_(self.linear_qkv.bias, 0.)
        nn.init.constant_(self.final_linear.bias, 0.)

    def forward(self, query, memory, mask):
        def split_head(x):
            # B x L x D => B x h x L x d
            return x.view(x.size(0), -1, self.head_count, self.dim_per_head).transpose(1, 2)

        def combine_head(x):
            # B x h x L x d  => B x L x D
            return x.transpose(1, 2).contiguous().view(x.size(0), -1, self.head_count * self.dim_per_head)

        if memory is None:
            q, k, v = torch.chunk(self.linear_qkv(query), 3, dim=-1)
        else:
            q = self.linear_q(query)
            k, v = torch.chunk(self.linear_kv(memory), 2, dim=-1)

        # 1) Project q, k, v.
        q = split_head(q)
        k = split_head(k)
        v = split_head(v)

        # 2) Calculate and scale scores.
        q = q * self.dim_per_head ** -0.5
        scores = torch.matmul(q, k.transpose(2, 3))

        mask = mask.unsqueeze(1).expand_as(scores)
        scores = scores.masked_fill(mask, -1e20)

        # 3) Apply attention dropout and compute context vectors.
        weights = self.dropout(self.softmax(scores))
        context = combine_head(torch.matmul(weights, v))

        return self.final_linear(context)
