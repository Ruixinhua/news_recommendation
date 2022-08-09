import torch
import torch.nn as nn

from news_recommendation.models.nrs.rs_base import MindNRSBase


class BATMRSModel(MindNRSBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.variant_name = kwargs.pop("variant_name", "base")
        topic_dim = self.head_num * self.head_dim
        # the structure of basic model
        self.final = nn.Linear(self.embedding_dim, self.embedding_dim)
        if self.variant_name == "base" or self.variant_name == "base_att":
            self.topic_layer = nn.Sequential(nn.Linear(self.embedding_dim, topic_dim), nn.Tanh(),
                                             nn.Linear(topic_dim, self.head_num))
            self.user_encode_layer = nn.GRU(self.embedding_dim, self.embedding_dim, batch_first=True, bidirectional=False)
        elif self.variant_name == "bi_batm":
            self.topic_layer = nn.Sequential(nn.Linear(self.embedding_dim, topic_dim), nn.Tanh(),
                                             nn.Linear(topic_dim, self.head_num))
            self.user_encode_layer = nn.Sequential(nn.Linear(self.embedding_dim, topic_dim), nn.Tanh(),
                                                   nn.Linear(topic_dim, self.head_num))
            self.user_final = nn.Linear(self.embedding_dim, self.embedding_dim)
        self.dropouts = nn.Dropout(self.dropout_rate)

    def extract_topic(self, input_feat):
        embedding = self.dropouts(self.embedding_layer(input_feat["news"]))
        topic_weight = self.topic_layer(embedding).transpose(1, 2)  # (N, H, S)
        # expand mask to the same size as topic weights
        mask = input_feat["news_mask"].expand(self.head_num, embedding.size(0), -1).transpose(0, 1) == 0
        topic_weight = torch.softmax(topic_weight.masked_fill(mask, -1e9), dim=-1)  # fill zero entry with -INF
        topic_vec = self.final(torch.matmul(topic_weight, embedding))  # (N, H, E)
        return topic_vec, topic_weight

    def news_encoder(self, input_feat):
        """input_feat: Size is [N * H, S]"""
        y, topic_weight = self.extract_topic(input_feat)
        y = self.dropouts(y)  # TODO dropout layer
        # add activation function
        return self.news_att_layer(y)[0]

    def user_encoder(self, input_feat):
        y = input_feat["history_news"]
        if self.variant_name == "base":
            y = self.user_encode_layer(y)[0]
            y = self.user_att_layer(y)[0]  # additive attention layer
        elif self.variant_name == "bi_batm":
            user_weight = self.user_encode_layer(y).transpose(1, 2)
            # mask = input_feat["news_mask"].expand(self.head_num, y.size(0), -1).transpose(0, 1) == 0
            # user_weight = torch.softmax(user_weight.masked_fill(mask, -1e9), dim=-1)  # fill zero entry with -INF
            user_vec = self.user_final(torch.matmul(user_weight, y))
            y = self.user_att_layer(user_vec)[0]  # additive attention layer
        elif self.variant_name == "base_att":
            y = self.user_att_layer(y)[0]  # additive attention layer
        return y