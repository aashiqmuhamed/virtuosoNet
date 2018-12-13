import torch
import torch.nn as nn
from torch.autograd import Variable
import pickle
import argparse
import math
import numpy as np
import asyncio
import shutil
import os
import time
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import performanceWorm
import copy
import random
import xml_matching


parser = argparse.ArgumentParser()
parser.add_argument("-mode", "--sessMode", type=str, default='train', help="train or test")
# parser.add_argument("-model", "--nnModel", type=str, default="cnn", help="cnn or fcn")
parser.add_argument("-path", "--testPath", type=str, default="./test_pieces/mozart545-1/", help="folder path of test mat")
# parser.add_argument("-tset", "--trainingSet", type=str, default="dataOneHot", help="training set folder path")
parser.add_argument("-data", "--dataName", type=str, default="graph_mozart", help="dat file name")
parser.add_argument("--resume", type=str, default="gcn_alpha_best.pth.tar", help="best model path")
parser.add_argument("-tempo", "--startTempo", type=int, default=0, help="start tempo. zero to use xml first tempo")
parser.add_argument("-trill", "--trainTrill", type=bool, default=False, help="train trill")
parser.add_argument("--beatTempo", type=bool, default=True, help="cal tempo from beat level")
parser.add_argument("-voice", "--voiceNet", type=bool, default=True, help="network in voice level")
parser.add_argument("-vel", "--velocity", type=str, default='50,65', help="mean velocity of piano and forte")
parser.add_argument("-dev", "--device", type=int, default=1, help="cuda device number")
parser.add_argument("-code", "--modelCode", type=str, default='gcn_alpha', help="code name for saving the model")
parser.add_argument("-comp", "--composer", type=str, default='Chopin', help="composer name of the input piece")

args = parser.parse_args()


class NetParams:
    class Param:
        def __init__(self):
            self.size = 0
            self.layer = 0
            self.input = 0

    def __init__(self):
        self.note = self.Param()
        self.onset = self.Param()
        self.beat = self.Param()
        self.measure = self.Param()
        self.final = self.Param()
        self.voice = self.Param()
        self.sum = self.Param()
        self.encoder = self.Param()
        self.input_size = 0
        self.output_size = 0

### parameters
NET_PARAM = NetParams()

NET_PARAM.note.layer = 2
NET_PARAM.note.size = 64
NET_PARAM.beat.layer = 2
NET_PARAM.beat.size = 32
NET_PARAM.measure.layer = 1
NET_PARAM.measure.size = 16
NET_PARAM.final.layer = 1
NET_PARAM.final.size = 24
NET_PARAM.voice.layer = 2
NET_PARAM.voice.size = 0
NET_PARAM.sum.layer = 2
NET_PARAM.sum.size = 64

NET_PARAM.encoder.size = 64
NET_PARAM.encoder.layer = 2

learning_rate = 0.0003
time_steps = 500
print('Learning Rate and Time Steps are ', learning_rate, time_steps)
num_epochs = 150
num_key_augmentation = 1

SCORE_INPUT = 57 #score information only
TOTAL_OUTPUT = 16
NET_PARAM.input_size = SCORE_INPUT
training_ratio = 0.8
DROP_OUT = 0.25

num_prime_param = 11
num_second_param = 0
num_trill_param = 5
num_voice_feed_param = 0 # velocity, onset deviation
num_tempo_info = 0
num_dynamic_info = 0 # distance from marking, dynamics vector 4, mean_piano, forte marking and velocity = 4
is_trill_index_score = -10
is_trill_index_concated = -10 - (num_prime_param + num_second_param)
NET_PARAM.output_size = num_prime_param


QPM_INDEX = 0
# VOICE_IDX = 11
TEMPO_IDX = 27
PITCH_IDX = 14
qpm_primo_index = 5
tempo_primo_index = -2
GRAPH_KEYS = ['onset', 'forward', 'melisma', 'rest', 'voice', 'boundary', 'closest']
N_EDGE_TYPE = len(GRAPH_KEYS) * 2
# mean_vel_start_index = 7
# vel_vec_start_index = 33

batch_size = 1

torch.cuda.set_device(args.device)
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

NET_PARAM.final.input = (NET_PARAM.note.size + NET_PARAM.beat.size +
                        NET_PARAM.measure.size ) * 2 + NET_PARAM.encoder.size + \
                        num_tempo_info + num_dynamic_info
NET_PARAM.encoder.input = (NET_PARAM.note.size + NET_PARAM.beat.size +
                           NET_PARAM.measure.size + NET_PARAM.voice.size) * 2 \
                          + num_prime_param
# if args.trainTrill is False:
#     NET_PARAM.final.input -= num_trill_param
if args.voiceNet:
    NET_PARAM.final.input += NET_PARAM.voice.size * 2

Second_NET_PARAM = copy.deepcopy(NET_PARAM)
Second_NET_PARAM.input_size = SCORE_INPUT + NET_PARAM.output_size
Second_NET_PARAM.output_size = num_second_param
Second_NET_PARAM.final.input += Second_NET_PARAM.output_size - NET_PARAM.output_size - num_tempo_info - num_voice_feed_param - num_dynamic_info

TrillNET_Param = copy.deepcopy(NET_PARAM)
TrillNET_Param.input_size = SCORE_INPUT + NET_PARAM.output_size + Second_NET_PARAM.output_size
TrillNET_Param.output_size = num_trill_param
TrillNET_Param.note.size = NET_PARAM.note.size * 2 + NET_PARAM.output_size + Second_NET_PARAM.output_size
TrillNET_Param.note.layer = 3

### Model
class GatedGraph(nn.Module):
    class subGraph():
        def __init__(self, size):
            self.wz = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
            self.wr = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
            self.wh = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
            nn.init.xavier_normal_(self.wz)
            nn.init.xavier_normal_(self.wr)
            nn.init.xavier_normal_(self.wh)

    def  __init__(self, size, num_edge_style):
        super(GatedGraph, self).__init__()
        self.sub = []
        # for i in range(num_edge_style):
        #     subgraph = self.subGraph(size)
        #     self.sub.append(subgraph)

        self.wz = torch.nn.Parameter(torch.Tensor(num_edge_style,size,size))
        self.wr = torch.nn.Parameter(torch.Tensor(num_edge_style,size,size))
        self.wh = torch.nn.Parameter(torch.Tensor(num_edge_style,size,size))
        self.uz = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
        self.bz = torch.nn.Parameter(torch.Tensor(size)).to(device)
        self.ur = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
        self.br = torch.nn.Parameter(torch.Tensor(size)).to(device)
        self.uh = torch.nn.Parameter(torch.Tensor(size, size)).to(device)
        self.bh = torch.nn.Parameter(torch.Tensor(size)).to(device)

        nn.init.xavier_normal_(self.wz)
        nn.init.xavier_normal_(self.wr)
        nn.init.xavier_normal_(self.wh)
        nn.init.xavier_normal_(self.uz)
        nn.init.xavier_normal_(self.ur)
        nn.init.xavier_normal_(self.uh)
        nn.init.zeros_(self.bz)
        nn.init.zeros_(self.br)
        nn.init.zeros_(self.bh)

        self.sigmoid = torch.nn.Sigmoid()
        self.tanh = torch.nn.Tanh()

    def forward(self, input, edge_matrix, iteration=5):

        for i in range(iteration):
            activation = torch.matmul(edge_matrix.transpose(1,2), input)
            temp_z = self.sigmoid( torch.bmm(activation, self.wz).sum(0) + torch.matmul(input, self.uz) + self.bz)
            temp_r = self.sigmoid( torch.bmm(activation, self.wr).sum(0) + torch.matmul(input, self.ur) + self.br)
            temp_hidden = self.tanh(torch.bmm(activation, self.wh).sum(0) + torch.matmul(temp_r * input, self.uh) + self.bh)

            input = (1 - temp_z) * input + temp_r * temp_hidden

        return input




class HAN_VAE(nn.Module):
    def __init__(self, network_parameters, num_trill_param=5):
        super(HAN_VAE, self).__init__()
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size
        self.num_layers = network_parameters.note.layer
        self.note_hidden_size = network_parameters.note.size
        self.num_beat_layers = network_parameters.beat.layer
        self.beat_hidden_size = network_parameters.beat.size
        self.num_measure_layers = network_parameters.measure.layer
        self.measure_hidden_size = network_parameters.measure.size
        self.final_hidden_size = network_parameters.final.size
        self.num_voice_layers = network_parameters.voice.layer
        self.voice_hidden_size = network_parameters.voice.size
        self.final_input = network_parameters.final.input
        self.encoder_size = network_parameters.encoder.size
        self.encoder_input_size = network_parameters.encoder.input
        self.encoder_layer_num = network_parameters.encoder.layer
        self.onset_hidden_size = network_parameters.onset.size
        self.num_onset_layers = network_parameters.onset.layer

        self.beat_attention = nn.Linear(self.note_hidden_size * 2, self.note_hidden_size * 2)
        self.beat_rnn = nn.LSTM(self.note_hidden_size * 2, self.beat_hidden_size, self.num_beat_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        self.measure_attention = nn.Linear(self.beat_hidden_size*2, self.beat_hidden_size*2)
        self.measure_rnn = nn.LSTM(self.beat_hidden_size * 2, self.measure_hidden_size, self.num_measure_layers, batch_first=True, bidirectional=True)
        # self.tempo_attention = nn.Linear(self.output_size-1, self.output_size-1)

        self.beat_tempo_forward = nn.LSTM(self.beat_hidden_size*2 + 3+ 3 + self.encoder_size, self.beat_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        self.beat_tempo_fc = nn.Linear(self.beat_hidden_size,  1)

        self.output_lstm = nn.LSTM(self.final_input, self.final_hidden_size, num_layers=1, batch_first=True, bidirectional=False)
        if args.beatTempo:
            # self.fc = nn.Linear(self.final_hidden_size * 2, self.output_size -1)
            self.fc = nn.Linear(self.final_hidden_size, self.output_size - 1)
        else:
            self.fc = nn.Linear(self.final_hidden_size , self.output_size)

        self.note_fc = nn.Sequential(
            nn.Linear(self.input_size, self.note_hidden_size),
            nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU(),
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU(),
        )

        self.graph_1st = GatedGraph(self.note_hidden_size, N_EDGE_TYPE)
        self.graph_between = nn.Sequential(
            nn.Linear(self.note_hidden_size, self.note_hidden_size),
            nn.BatchNorm1d(self.note_hidden_size),
            nn.ReLU()
        )
        self.graph_2nd = GatedGraph(self.note_hidden_size, N_EDGE_TYPE)


        self.performance_encoder = nn.LSTM(self.encoder_input_size, self.encoder_size,  num_layers=self.encoder_layer_num, batch_first=True, bidirectional=False)
        self.performance_encoder_mean = nn.Linear(self.encoder_size, self.encoder_size)
        self.performance_encoder_var = nn.Linear(self.encoder_size, self.encoder_size)

        self.softmax = nn.Softmax(dim=0)
        self.sigmoid = nn.Sigmoid()
        self.tanh = nn.Tanh()



    def forward(self, x, y, edges, note_locations, start_index, step_by_step = False, initial_z=False, rand_threshold=0.7):
        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]
        onset_numbers = [x.onset for x in note_locations]
        num_notes = x.size(1)

        note_out, beat_hidden_out, measure_hidden_out = \
            self.run_offline_score_model(x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index)
        beat_out_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
        measure_out_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)
        if initial_z:
            perform_z = torch.Tensor(initial_z).to(device).view(1,-1)
            perform_mu = 0
            perform_var = 0
        else:
            perform_concat = torch.cat((note_out, beat_out_spanned, measure_out_spanned, y), 2)
            perform_style_encoded, _ = self.performance_encoder(perform_concat)
            # perform_style_reduced = perform_style_reduced.view(-1,self.encoder_input_size)
            # perform_style_node = self.sum_with_attention(perform_style_reduced, self.perform_attention)
            perform_style_vector = perform_style_encoded[:, -1, :]  # need check
            perform_z, perform_mu, perform_var = \
                self.encode_with_net(perform_style_vector, self.performance_encoder_mean, self.performance_encoder_var)

        # perform_z = self.performance_decoder(perform_z)
        perform_z_batched = perform_z.repeat(x.shape[1], 1).view(1,x.shape[1], -1)
        num_notes = x.size(1)

        tempo_hidden = self.init_beat_tempo_forward(x.size(0))
        final_hidden = self.init_final_layer(x.size(0))

        num_notes = x.size(1)
        num_beats = beat_hidden_out.size(1)

        # non autoregressive

        qpm_primo = x[:,:,qpm_primo_index].view(1,-1,1)
        tempo_primo = x[:,:,tempo_primo_index:].view(1,-1,2)
        if args.beatTempo:
            # beat_tempos = self.note_tempo_infos_to_beat(y, beat_numbers, start_index, QPM_INDEX)
            beat_qpm_primo = qpm_primo[0,0,0].repeat((1, num_beats, 1))
            beat_tempo_primo = tempo_primo[0,0,:].repeat((1, num_beats, 1))
            beat_tempo_vector = self.note_tempo_infos_to_beat(x, beat_numbers, start_index, TEMPO_IDX)
            if 'beat_hidden_out' not in locals():
                beat_hidden_out = beat_out_spanned
            num_beats = beat_hidden_out.size(1)
            # score_z_beat_spanned = score_z.repeat(num_beats,1).view(1,num_beats,-1)
            perform_z_beat_spanned = perform_z.repeat(num_beats,1).view(1,num_beats,-1)
            beat_tempo_cat = torch.cat((beat_hidden_out, beat_qpm_primo, beat_tempo_primo, beat_tempo_vector, perform_z_beat_spanned), 2)
            beat_forward, tempo_hidden = self.beat_tempo_forward(beat_tempo_cat, tempo_hidden)
            tempos = self.beat_tempo_fc(beat_forward)
            num_notes = note_out.size(1)
            tempos_spanned = self.span_beat_to_note_num(tempos, beat_numbers, num_notes, start_index)
            # y[0, :, 0] = tempos_spanned.view(-1)



        # mean_velocity_info = x[:, :, mean_vel_start_index:mean_vel_start_index+4].view(1,-1,4)
        # dynamic_info = torch.cat((x[:, :, mean_vel_start_index + 4].view(1,-1,1),
        #                           x[:, :, vel_vec_start_index:vel_vec_start_index + 4]), 2).view(1,-1,5)

        out_combined = torch.cat((
            note_out, beat_out_spanned, measure_out_spanned,
            # qpm_primo, tempo_primo, mean_velocity_info, dynamic_info,
            perform_z_batched), 2)

        out, final_hidden = self.output_lstm(out_combined, final_hidden)

        out = self.fc(out)
        # out = torch.cat((out, trill_out), 2)

        if args.beatTempo:
            out = torch.cat((tempos_spanned, out), 2)



        return out, perform_mu, perform_var, note_out

    def run_offline_score_model(self, x, edges, onset_numbers, beat_numbers, measure_numbers, voice_numbers, start_index):
        x = x[0,:,:]
        beat_hidden = self.init_beat_layer(1)
        measure_hidden = self.init_measure_layer(1)

        note_out = self.run_graph_network(x, edges, start_index)
        note_out = note_out.view(1,note_out.shape[0], note_out.shape[1])
        # note_out, onset_out = self.run_onset_rnn(x, voice_out, onset_numbers, start_index)
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)
        # beat_nodes = self.make_higher_node(onset_out, self.beat_attention, onset_numbers, beat_numbers, start_index)
        beat_nodes = self.make_beat_node(note_out, beat_numbers, start_index)
        beat_hidden_out, beat_hidden = self.beat_rnn(beat_nodes, beat_hidden)
        measure_nodes = self.make_higher_node(beat_hidden_out, self.measure_attention, beat_numbers, measure_numbers, start_index)
        # measure_nodes = self.make_measure_node(beat_hidden_out, measure_numbers, beat_numbers, start_index)
        measure_hidden_out, measure_hidden = self.measure_rnn(measure_nodes, measure_hidden)

        return note_out, beat_hidden_out, measure_hidden_out

    def run_graph_network(self, nodes, graph_matrix, start_index):
        # 1. Run feed-forward network by note level

        notes_hidden = self.note_fc(nodes)
        num_notes = notes_hidden.size(1)

        notes_hidden = self.graph_1st(notes_hidden, graph_matrix)
        time3 = time.time()

        notes_between = self.graph_between(notes_hidden)

        notes_hidden_second = self.graph_2nd(notes_between, graph_matrix)

        notes_hidden = torch.cat((notes_hidden, notes_hidden_second),-1)

        return notes_hidden


    def encode_with_net(self, score_input, mean_net, var_net):
        mu = mean_net(score_input)
        var = var_net(score_input)

        z = self.reparameterize(mu, var)
        return z, mu, var

    def reparameterize(self, mu, logvar):
        std = torch.exp(0.5*logvar)
        eps = torch.randn_like(std)
        return eps.mul(std).add_(mu)

    # def decode_with_net(self, z, decode_network):
    #     decode_network
    #     return

    def sum_with_attention(self, hidden, attention_net):
        attention = attention_net(hidden)
        attention = self.softmax(attention)
        upper_node = hidden * attention
        upper_node_sum = torch.sum(upper_node, dim=0)

        return upper_node_sum

    def make_higher_node(self, lower_out, attention_weights, lower_indexes, higher_indexes, start_index):
        higher_nodes = []
        prev_higher_index = higher_indexes[start_index]
        lower_node_start = 0
        lower_node_end = 0
        num_lower_nodes = lower_out.shape[1]
        start_lower_index = lower_indexes[start_index]
        lower_hidden_size = lower_out.shape[2]
        for low_index in range(num_lower_nodes):
            absolute_low_index = start_lower_index + low_index
            current_note_index = lower_indexes.index(absolute_low_index)

            if higher_indexes[current_note_index] > prev_higher_index:
                # new beat start
                lower_node_end = low_index
                corresp_lower_out = lower_out[0, lower_node_start:lower_node_end, :]
                higher = self.sum_with_attention(corresp_lower_out, attention_weights)
                higher_nodes.append(higher)

                lower_node_start = low_index
                prev_higher_index = higher_indexes[current_note_index]

        corresp_lower_out = lower_out[0, lower_node_start:, :]
        higher = self.sum_with_attention(corresp_lower_out, attention_weights)
        higher_nodes.append(higher)

        higher_nodes = torch.stack(higher_nodes).view(1, -1, lower_hidden_size)

        return higher_nodes


    def make_beat_node(self, hidden_out, beat_number, start_index):
        beat_nodes = []
        prev_beat = beat_number[start_index]
        beat_notes_start = 0
        beat_notes_end = 0
        num_notes = hidden_out.shape[1]
        for note_index in range(num_notes):
            actual_index = start_index + note_index
            if beat_number[actual_index] > prev_beat:
                #new beat start
                beat_notes_end = note_index
                corresp_hidden = hidden_out[0, beat_notes_start:beat_notes_end, :]
                beat = self.sum_with_attention(corresp_hidden, self.beat_attention)
                beat_nodes.append(beat)

                beat_notes_start = note_index
                prev_beat = beat_number[actual_index]

        last_hidden =  hidden_out[0, beat_notes_end:, :]
        beat = self.sum_with_attention(last_hidden, self.beat_attention)
        beat_nodes.append(beat)

        beat_nodes = torch.stack(beat_nodes).view(1, -1, self.note_hidden_size * 2)
        # beat_nodes = torch.Tensor(beat_nodes)

        return beat_nodes

    def make_measure_node(self, beat_out, measure_number, beat_number, start_index):
        measure_nodes = []
        prev_measure = measure_number[start_index]
        measure_beats_start = 0
        measure_beats_end = 0
        num_beats = beat_out.shape[1]
        start_beat = beat_number[start_index]
        for beat_index in range(num_beats):
            current_beat = start_beat + beat_index
            current_note_index = beat_number.index(current_beat)

            if measure_number[current_note_index] > prev_measure:
                # new beat start
                measure_beats_end = beat_index
                corresp_hidden = beat_out[0, measure_beats_start:measure_beats_end, :]
                measure = self.sum_with_attention(corresp_hidden, self.measure_attention)
                measure_nodes.append(measure)

                measure_beats_start = beat_index
                prev_measure = measure_number[beat_index]

        last_hidden = beat_out[0, measure_beats_end:, :]
        measure = self.sum_with_attention(last_hidden, self.measure_attention)
        measure_nodes.append(measure)

        measure_nodes = torch.stack(measure_nodes).view(1,-1,self.beat_hidden_size*2)

        return measure_nodes

    def span_beat_to_note_num(self, beat_out, beat_number, num_notes, start_index):
        start_beat = beat_number[start_index]
        num_beat = beat_out.shape[1]
        span_mat = torch.zeros(1, num_notes, num_beat)
        node_size = beat_out.shape[2]
        for i in range(num_notes):
            beat_index = beat_number[start_index+i] - start_beat
            if beat_index >= num_beat:
                beat_index = num_beat-1
            span_mat[0,i,beat_index] = 1
        span_mat = span_mat.to(device)

        spanned_beat = torch.bmm(span_mat, beat_out)
        return spanned_beat

    def note_tempo_infos_to_beat(self, y, beat_numbers, start_index, index=None):
        beat_tempos = []
        num_notes = y.size(1)
        prev_beat = -1
        for i in range(num_notes):
            cur_beat = beat_numbers[start_index+i]
            if cur_beat > prev_beat:
                if index is None:
                    beat_tempos.append(y[0,i,:])
                if index == TEMPO_IDX:
                    beat_tempos.append(y[0,i,TEMPO_IDX:TEMPO_IDX+3])
                else:
                    beat_tempos.append(y[0,i,index])
                prev_beat = cur_beat
        num_beats = len(beat_tempos)
        beat_tempos = torch.stack(beat_tempos).view(1,num_beats,-1)
        return beat_tempos


    def run_voice_net(self, batch_x, voice_hidden, voice_numbers, max_voice):
        num_notes = batch_x.size(1)
        output = torch.zeros(1, batch_x.size(1), self.voice_hidden_size * 2).to(device)
        voice_numbers = torch.Tensor(voice_numbers)
        for i in range(1,max_voice+1):
            voice_x_bool = voice_numbers == i
            num_voice_notes = torch.sum(voice_x_bool)
            if num_voice_notes > 0:
                span_mat = torch.zeros(num_notes, num_voice_notes)
                note_index_in_voice = 0
                for j in range(num_notes):
                    if voice_x_bool[j] ==1:
                        span_mat[j, note_index_in_voice] = 1
                        note_index_in_voice += 1
                span_mat = span_mat.view(1,num_notes,-1).to(device)
                voice_x = batch_x[0,voice_x_bool,:].view(1,-1, self.input_size)
                ith_hidden = voice_hidden[i-1]

                ith_voice_out, ith_hidden = self.voice_net(voice_x, ith_hidden)
                # ith_voice_out, ith_hidden = self.lstm(voice_x, ith_hidden)
                output += torch.bmm(span_mat, ith_voice_out)
        return output, voice_hidden

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.note_hidden_size).to(device)
        return (h0, h0)

    def init_final_layer(self, batch_size):
        h0 = torch.zeros(1 , batch_size, self.final_hidden_size).to(device)
        return (h0, h0)

    def init_onset_layer(self, batch_size):
        h0 = torch.zeros(self.num_onset_layers * 2, batch_size, self.onset_hidden_size).to(device)
        return (h0, h0)

    def init_beat_layer(self, batch_size):
        h0 = torch.zeros(self.num_beat_layers * 2, batch_size, self.beat_hidden_size).to(device)
        return (h0, h0)

    def init_measure_layer(self, batch_size):
        h0 = torch.zeros(self.num_measure_layers * 2, batch_size, self.measure_hidden_size).to(device)
        return (h0, h0)

    def init_beat_tempo_forward(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.beat_hidden_size).to(device)
        return (h0, h0)

    def init_voice_layer(self, batch_size, max_voice):
        layers = []
        for i in range(max_voice):
            # h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.voice_hidden_size).to(device)
            h0 = torch.zeros(self.num_voice_layers * 2, batch_size, self.note_hidden_size).to(device)
            layers.append((h0, h0))
        return layers

    def init_onset_encoder(self, batch_size):
        h0 = torch.zeros(1, batch_size, self.onset_hidden_size).to(device)
        return (h0, h0)


class ExtraHANVAE(HAN_VAE):
    def __init__(self, network_parameters):
        super(ExtraHANVAE, self).__init__(network_parameters)
        self.fc = nn.Linear(self.final_hidden_size, self.output_size)

    def forward(self, x, y, note_locations, start_index, step_by_step=False, rand_threshold = 0.7):

        beat_numbers = [x.beat for x in note_locations]
        measure_numbers = [x.measure for x in note_locations]
        voice_numbers = [x.voice for x in note_locations]

        hidden_out, beat_hidden_out, measure_hidden_out = \
            self.run_offline_score_model(x, beat_numbers, measure_numbers, voice_numbers, start_index)
        num_notes = x.size(1)
        if not step_by_step:
            beat_hidden_spanned = self.span_beat_to_note_num(beat_hidden_out, beat_numbers, num_notes, start_index)
            measure_hidden_spanned = self.span_beat_to_note_num(measure_hidden_out, measure_numbers, num_notes, start_index)

        if step_by_step:
            num_notes = x.size(1)
            final_hidden = self.init_final_layer(1)

            prev_out = y[0, 0, :]
            out_total = torch.zeros(num_notes, self.output_size).to(device)
            is_valid = y.size(1) > 1
            for i in range(num_notes):
                if is_valid and i > 0 and random.random() > rand_threshold:
                    out = y[0, i - 1, :]
                else:
                    corresp_beat = beat_numbers[start_index + i] - beat_numbers[start_index]
                    corresp_measure = measure_numbers[start_index + i] - measure_numbers[start_index]
                    out_combined = torch.cat(
                        (hidden_out[0, i, :], beat_hidden_out[0, corresp_beat, :],
                         measure_hidden_out[0, corresp_measure, :], prev_out)).view(1, 1, -1)

                    out, final_hidden = self.output_lstm(out_combined, final_hidden)
                    out = out.view(-1)
                    out = self.fc(out)

                prev_out = out
                out_total[i, :] = out
            out_total = out_total.view(1,num_notes,-1)
            return out_total

class TrillRNN(nn.Module):
    def __init__(self, network_parameters):
        super(TrillRNN, self).__init__()
        self.hidden_size = network_parameters.note.size
        self.num_layers = network_parameters.note.layer
        self.input_size = network_parameters.input_size
        self.output_size = network_parameters.output_size

        # self.lstm = nn.LSTM(self.input_size, self.hidden_size, self.num_layers, batch_first=True, bidirectional=True, dropout=DROP_OUT)
        # self.fc = nn.Linear(hidden_size * 2, num_output)  # 2 for bidirection
        self.fc = nn.Sequential(
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.hidden_size),
            nn.ReLU(),
            nn.Linear(self.hidden_size, self.output_size),
            nn.ReLU()
        )
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, note_hidden):
        # hidden = self.init_hidden(x.size(0))
        # hidden_out, hidden = self.lstm(x, hidden)  # out: tensor of shape (batch_size, seq_length, hidden_size*2)

        # Decode the hidden state of the last time step
        note_hidden = torch.nn.Parameter(note_hidden, requires_grad=False)
        is_trill_mat = x[:, :, is_trill_index_concated]
        is_trill_mat = is_trill_mat.view(1,-1,1).repeat(1,1,num_trill_param).view(1,-1,num_trill_param)
        is_trill_mat = Variable(is_trill_mat, requires_grad=False)
        out = self.fc(note_hidden)
        up_trill = self.sigmoid(out[:,:,-1])
        out[:,:,-1] = up_trill
        out = out * is_trill_mat
        return out

    def init_hidden(self, batch_size):
        h0 = torch.zeros(self.num_layers * 2, batch_size, self.hidden_size).to(device)
        return (h0, h0)

def vae_loss(recon_x, x, mu, logvar):
    MSE = nn.MSELoss(recon_x, x.view(-1, 784), reduction='sum')

    # see Appendix B from VAE paper:
    # Kingma and Welling. Auto-Encoding Variational Bayes. ICLR, 2014
    # https://arxiv.org/abs/1312.6114
    # 0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    KLD = -0.5 * torch.sum(1 + logvar - mu.pow(2) - logvar.exp())

    return MSE + KLD


# model = BiRNN(input_size, hidden_size, num_layers, num_output).to(device)
model = HAN_VAE(NET_PARAM).to(device)
# second_model = ExtraHAN(NET_PARAM).to(device)
trill_model =TrillRNN(TrillNET_Param).to(device)


criterion = nn.MSELoss()
optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
# second_optimizer = torch.optim.Adam(second_model.parameters(), lr=learning_rate)
trill_optimizer = torch.optim.Adam(trill_model.parameters(), lr=learning_rate)


def save_checkpoint(state, is_best, filename=args.modelCode, model_name='prime'):
    save_name = model_name + '_' + filename + '_checkpoint.pth.tar'
    torch.save(state, save_name)
    if is_best:
        best_name = model_name + '_' + filename + '_best.pth.tar'
        shutil.copyfile(save_name, best_name)


def key_augmentation(data_x, key_change):
    # key_change = 0
    data_x_aug = copy.deepcopy(data_x)
    pitch_start_index = PITCH_IDX
    # while key_change == 0:
    #     key_change = random.randrange(-5, 7)
    for data in data_x_aug:
        octave = data[pitch_start_index]
        pitch_class_vec = data[pitch_start_index+1:pitch_start_index+13]
        pitch_class = pitch_class_vec.index(1)
        new_pitch = pitch_class + key_change
        if new_pitch < 0:
            octave -= 0.25
        elif new_pitch > 12:
            octave += 0.25
        new_pitch = new_pitch % 12

        new_pitch_vec = [0] * 13
        new_pitch_vec[0] = octave
        new_pitch_vec[new_pitch+1] = 1

        data[pitch_start_index: pitch_start_index+13] = new_pitch_vec

    return data_x_aug

def edges_to_matrix(edges, num_notes):
    num_keywords = len(GRAPH_KEYS)
    matrix = np.zeros((N_EDGE_TYPE, num_notes, num_notes))

    for edg in edges:
        edge_type = GRAPH_KEYS.index(edg[2])
        matrix[edge_type, edg[0], edg[1]] = 1
        if edge_type != 0:
            matrix[edge_type+num_keywords, edg[1], edg[0]] = 1

    matrix[num_keywords, :,:] = np.identity(num_notes)

    # for k in range(num_keywords):
    #     selected_key = GRAPH_KEYS[k]
    #     selected_edge = edges[selected_key]
    #     for i in range(time_steps):
    #         abs_index = start_index + i
    #         for edge_index in selected_edge[abs_index]:
    #             if 0 <= edge_index - start_index < time_steps:
    #                 matrix[k, i, edge_index-start_index] = 1

    matrix = torch.sparse.FloatTensor(matrix).to(device)
    return matrix

def perform_xml(input, input_y, edges, note_locations, tempo_stats, valid_y = None, initial_z = False):
    # time1= time.time()
    with torch.no_grad():  # no need to track history in sampling
        model_eval = model.eval()
        prime_input_y = input_y[:,:,0:num_prime_param].view(1,-1,num_prime_param)
        prime_outputs, _, _, note_hidden_out = model_eval(input, prime_input_y, edges, note_locations=note_locations, start_index=0, step_by_step=False, initial_z=initial_z)
        # second_inputs = torch.cat((input,prime_outputs), 2)
        # second_input_y = input_y[:,:,num_prime_param:num_prime_param+num_second_param].view(1,-1,num_second_param)
        # model_eval = second_model.eval()
        # second_outputs = model_eval(second_inputs, second_input_y, note_locations, 0, step_by_step=True)
        if torch.sum(input[:,:,is_trill_index_score])> 0:
            trill_inputs = torch.cat((note_hidden_out,prime_outputs), 2)
            model_eval = trill_model.eval()
            trill_outputs = model_eval(trill_inputs)
        else:
            trill_outputs = torch.zeros(1, input.size(1), num_trill_param).to(device)

        outputs = torch.cat((prime_outputs, trill_outputs),2)
        return outputs


def batch_time_step_run(x,y,prev_feature, edges, note_locations, align_matched, step, batch_size=batch_size, time_steps=time_steps, model=model, trill_model=trill_model):
    num_total_notes = len(x)
    if step < total_batch_num - 1:
        batch_start = step * batch_size * time_steps
        batch_end = (step + 1) * batch_size * time_steps
        batch_x = torch.Tensor(x[batch_start:batch_end])
        batch_y = torch.Tensor(y[batch_start:batch_end])
        align_matched = torch.Tensor(align_matched[batch_start:batch_end])
        # input_y = torch.Tensor(prev_feature[batch_start:batch_end])
        # input_y = torch.cat((zero_tensor, batch_y[0:batch_size * time_steps-1]), 0).view((batch_size, time_steps,num_output)).to(device)
    else:
        # num_left_data = data_size % batch_size*time_steps
        batch_start = num_total_notes-(batch_size * time_steps)
        batch_x = torch.Tensor(x[batch_start:])
        batch_y = torch.Tensor(y[batch_start:])
        align_matched = torch.Tensor(align_matched[batch_start:])
        # input_y = torch.Tensor(prev_feature[batch_start:])
        # input_y = torch.cat((zero_tensor, batch_y[0:batch_size * time_steps-1]), 0).view((batch_size, time_steps,num_output)).to(device)
    batch_x = batch_x.view((batch_size, time_steps, SCORE_INPUT)).to(device)
    batch_y = batch_y.view((batch_size, time_steps, TOTAL_OUTPUT)).to(device)
    align_matched = align_matched.view((batch_size, time_steps, 1)).to(device)
    align_matched = align_matched.repeat(1,1,num_prime_param)
    # input_y = input_y.view((batch_size, time_steps, TOTAL_OUTPUT)).to(device)

    # async def train_prime(batch_x, batch_y, input_y, model):
    prime_batch_x = batch_x
    prime_batch_y = batch_y[:,:,0:num_prime_param]
    prime_batch_y *= align_matched

    batch_graph = edges[:,batch_start:batch_start+time_steps, batch_start:batch_start+time_steps]

    model_train = model.train()
    prime_outputs, perform_mu, perform_var, note_out \
        = model_train(prime_batch_x, prime_batch_y, batch_graph, note_locations, batch_start, step_by_step=False)
    prime_outputs *= align_matched

    tempo_loss = cal_tempo_loss_in_beat(prime_outputs, prime_batch_y, note_locations, batch_start)
    mse_loss = criterion(prime_outputs[:,:,1:], prime_batch_y[:,:,1:])
    perform_kld = -0.5 * torch.sum(1 + perform_var - perform_mu.pow(2) - perform_var.exp())
    prime_loss = tempo_loss + mse_loss + perform_kld
    optimizer.zero_grad()
    prime_loss.backward()
    torch.nn.utils.clip_grad_norm_(model.parameters(), 0.25)
    optimizer.step()

    if torch.sum(batch_x[:,:,is_trill_index_score]) > 0:
        trill_batch_x = torch.cat((batch_x, batch_y[:,:,0:num_prime_param+num_second_param]), 2)
        note_out_cat = torch.cat((note_out, batch_y[:,:,0:num_prime_param+num_second_param]),2)
        trill_batch_y = batch_y[:,:,-num_trill_param:]
        model_train = trill_model.train()
        trill_output = model_train(trill_batch_x, note_out_cat)
        trill_loss = criterion(trill_output, trill_batch_y)
        trill_optimizer.zero_grad()
        trill_loss.backward()
        torch.nn.utils.clip_grad_norm_(trill_model.parameters(), 0.25)
        trill_optimizer.step()
    else:
        trill_loss = torch.zeros(1)

    # loss = criterion(outputs, batch_y)
    # tempo_loss = criterion(prime_outputs[:, :, 0], prime_batch_y[:, :, 0])
    vel_loss = criterion(prime_outputs[:, :, 1], prime_batch_y[:, :, 1])
    dev_loss = criterion(prime_outputs[:, :, 2], prime_batch_y[:, :, 2])
    return tempo_loss, vel_loss, dev_loss, trill_loss, perform_kld

def cal_tempo_loss_in_beat(pred_x, true_x, note_locations, start_index):
    previous_beat = -1
    num_notes = pred_x.shape[1]
    start_beat = note_locations[start_index].beat
    num_beats = note_locations[num_notes+start_index-1].beat - start_beat + 1


    pred_beat_tempo = torch.zeros([num_beats]).to(device)
    true_beat_tempo = torch.zeros([num_beats]).to(device)
    for i in range(num_notes):
        current_beat = note_locations[i+start_index].beat
        if current_beat > previous_beat:
            previous_beat = current_beat
            pred_beat_tempo[current_beat-start_beat] = pred_x[0,i,QPM_INDEX]
            true_beat_tempo[current_beat-start_beat] = true_x[0,i,QPM_INDEX]

    tempo_loss = criterion(pred_beat_tempo, true_beat_tempo)

    return tempo_loss


### training

if args.sessMode == 'train':
    model_parameters = filter(lambda p: p.requires_grad, model.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print('Number of Network Parameters is ', params)


    # load data
    print('Loading the training data...')
    with open(args.dataName + ".dat", "rb") as f:
        u = pickle._Unpickler(f)
        u.encoding = 'latin1'
        # p = u.load()
        # complete_xy = pickle.load(f)
        complete_xy = u.load()

    with open(args.dataName + "_stat.dat", "rb") as f:
        u = pickle._Unpickler(f)
        u.encoding = 'latin1'
        means, stds = u.load()

    # perform_num = len(complete_xy)
    tempo_stats = [means[1][0], stds[1][0]]

    # train_perf_num = int(perform_num * training_ratio)
    train_xy = complete_xy['train']
    test_xy = complete_xy['valid']
    print('number of train performances: ', len(train_xy), 'number of valid perf: ', len(test_xy))

    print(train_xy[0][0][0])
    best_prime_loss = float("inf")
    best_second_loss = float("inf")
    best_trill_loss = float("inf")
    # total_step = len(train_loader)
    for epoch in range(num_epochs):
        tempo_loss_total =[]
        vel_loss_total =[]
        second_loss_total =[]
        trill_loss_total =[]
        kld_total = []
        for xy_tuple in train_xy:
            train_x = xy_tuple[0]
            train_y = xy_tuple[1]
            prev_feature = xy_tuple[2]
            note_locations = xy_tuple[3]
            align_matched = xy_tuple[4]
            edges = xy_tuple[5]

            data_size = len(train_x)
            graphs = edges_to_matrix(edges, data_size)
            total_batch_num = int(math.ceil(data_size / (time_steps * batch_size)))

            key_lists = [0]
            key = 0
            for i in range(num_key_augmentation):
                while key in key_lists:
                    key = random.randrange(-5, 7)
                key_lists.append(key)

            for i in range(num_key_augmentation+1):
                key = key_lists[i]
                temp_train_x = key_augmentation(train_x, key)

                for step in range(total_batch_num):
                    tempo_loss, vel_loss, second_loss, trill_loss, kld = \
                        batch_time_step_run(temp_train_x, train_y, prev_feature, graphs, note_locations, align_matched, step)
                    # optimizer.zero_grad()
                    # loss.backward()
                    # optimizer.step()
                    # print(tempo_loss)
                    tempo_loss_total.append(tempo_loss.item())
                    vel_loss_total.append(vel_loss.item())
                    second_loss_total.append(second_loss.item())
                    trill_loss_total.append(trill_loss.item())
                    kld_total.append(kld.item())

        print('Epoch [{}/{}], Loss - Tempo: {:.4f}, Vel: {:.4f}, Deviation: {:.4f}, Trill: {:.4f}, KLD: {:.4f}'
              .format(epoch + 1, num_epochs, np.mean(tempo_loss_total), np.mean(vel_loss_total),
                      np.mean(second_loss_total), np.mean(trill_loss_total), np.mean(kld_total) *1000))


        ## Validation
        valid_loss_total = []
        tempo_loss_total =[]
        vel_loss_total =[]
        second_loss_total =[]
        trill_loss_total =[]
        for xy_tuple in test_xy:
            test_x = xy_tuple[0]
            test_y = xy_tuple[1]
            prev_feature = xy_tuple[2]
            note_locations = xy_tuple[3]
            align_matched = xy_tuple[4]
            edges = xy_tuple[5]
            graphs = edges_to_matrix(edges, len(test_x))


            batch_x = torch.Tensor(test_x).view((1, -1, SCORE_INPUT)).to(device)
            batch_y = torch.Tensor(test_y).view((1, -1, TOTAL_OUTPUT)).to(device)
            input_y = torch.Tensor(prev_feature).view((1, -1, TOTAL_OUTPUT)).to(device)
            align_matched = torch.Tensor(align_matched).view(1, -1, 1).to(device)
            align_matched = align_matched.repeat(1,1,TOTAL_OUTPUT)
            # if args.trainTrill:
            #     input_y = torch.Tensor(prev_feature).view((1, -1, output_size)).to(device)
            # else:
            #     input_y = torch.Tensor(prev_feature)
            #     input_y = input_y[:,:-num_trill_param].view((1, -1, output_size - num_trill_param)).to(device)
            # hidden = model.init_hidden(1)
            # final_hidden = model.init_final_layer(1)
            # outputs, hidden, final_hidden = model(batch_x, input_y, hidden, final_hidden)

            # batch_x = Variable(torch.Tensor(test_x)).view((1, -1, SCORE_INPUT)).to(device)
            #
            outputs = perform_xml(batch_x, input_y, graphs, note_locations, tempo_stats, valid_y=batch_y)

            outputs *= align_matched
            batch_y *= align_matched
            # outputs = outputs.view(1,-1,NET_PARAM.output_size)
            # outputs = torch.Tensor(outputs).view((1, -1, output_size)).to(device)
            # if args.trainTrill:
            #     outputs = torch.Tensor(outputs).view((1, -1, output_size))
            # else:
            #     outputs = torch.Tensor(outputs).view((1, -1, output_size - num_trill_param))
            valid_loss = criterion(outputs[:,:,1:-num_trill_param], batch_y[:,:,1:-num_trill_param])
            tempo_loss = cal_tempo_loss_in_beat(outputs, batch_y, note_locations, 0)
            vel_loss = criterion(outputs[:,:,1], batch_y[:,:,1])
            second_loss = criterion(outputs[:,:,2],
                                    batch_y[:,:,2])
            trill_loss = criterion(outputs[:,:,-num_trill_param:], batch_y[:,:,-num_trill_param:])

            valid_loss_total.append(valid_loss.item())
            tempo_loss_total.append(tempo_loss.item())
            vel_loss_total.append(vel_loss.item())
            second_loss_total.append(second_loss.item())
            trill_loss_total.append(trill_loss.item())

        mean_valid_loss = np.mean(valid_loss_total)
        mean_tempo_loss = np.mean(tempo_loss_total)
        mean_valid_loss = (mean_valid_loss + mean_tempo_loss * 0.5) / 1.5
        mean_vel_loss =  np.mean(vel_loss_total)
        mean_second_loss = np.mean(second_loss_total)
        mean_trill_loss = np.mean(trill_loss_total)
        print("Valid Loss= {:.4f} , Tempo: {:.4f}, Vel: {:.4f}, Deviation: {:.4f}, Trill: {:.4f}"
              .format(mean_valid_loss, mean_tempo_loss , mean_vel_loss,
                      mean_second_loss, mean_trill_loss))

        mean_prime_loss = (mean_tempo_loss + mean_vel_loss + mean_second_loss) /3
        is_best = mean_valid_loss < best_prime_loss
        best_prime_loss = min(mean_valid_loss, best_prime_loss)


        is_best_trill = mean_trill_loss < best_trill_loss
        best_trill_loss = min(mean_trill_loss, best_trill_loss)


        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_valid_loss': best_prime_loss,
            'optimizer': optimizer.state_dict(),
        }, is_best, model_name='prime')
        save_checkpoint({
            'epoch': epoch + 1,
            'state_dict': trill_model.state_dict(),
            'best_valid_loss': best_trill_loss,
            'optimizer': trill_optimizer.state_dict(),
        }, is_best_trill, model_name='trill')


    #end of epoch



elif args.sessMode=='test':
### test session
    with open(args.dataName + "_stat.dat", "rb") as f:
        u = pickle._Unpickler(f)
        u.encoding = 'latin1'
        means, stds = u.load()
    if os.path.isfile('prime_'+args.resume):
        print("=> loading checkpoint '{}'".format(args.resume))
        model_codes = ['prime', 'trill']
        for i in range(2):
            filename = model_codes[i] + '_' + args.resume
            checkpoint = torch.load(filename)
            # args.start_epoch = checkpoint['epoch']
            # best_valid_loss = checkpoint['best_valid_loss']
            if i == 0:
                model.load_state_dict(checkpoint['state_dict'])
            elif i==1:
                trill_model.load_state_dict(checkpoint['state_dict'])
            # optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(filename, checkpoint['epoch']))
    else:
        print("=> no checkpoint found at '{}'".format(args.resume))
    path_name = args.testPath
    composer_name = args.composer
    vel_pair = (int(args.velocity.split(',')[0]), int(args.velocity.split(',')[1]))
    test_x, xml_notes, xml_doc, edges, note_locations = xml_matching.read_xml_to_array(path_name, means, stds, args.startTempo, composer_name, vel_pair)
    batch_x = torch.Tensor(test_x).to(device)
    batch_x = batch_x.view(1, -1, SCORE_INPUT)

    for i in range(len(stds)):
        for j in range(len(stds[i])):
            if stds[i][j] < 1e-4:
                stds[i][j] = 1
    #
    # test_x = np.asarray(test_x)
    # timestep_quantize_num = int(math.ceil(test_x.shape[0] / time_steps))
    # padding_size = timestep_quantize_num * time_steps - test_x.shape[0]
    # test_x_padded = np.pad(test_x, ((0, padding_size), (0, 0)), 'constant')
    # batch_x = test_x_padded.reshape((-1, time_steps, input_size))
    # batch_x = Variable(torch.from_numpy(batch_x)).float().to(device)
    # tempos = xml_doc.get_tempos()

    if args.startTempo == 0:
        start_tempo = xml_notes[0].state_fixed.qpm / 60 * xml_notes[0].state_fixed.divisions
        start_tempo = math.log(start_tempo, 10)
        # start_tempo_norm = (start_tempo - means[1][0]) / stds[1][0]
    else:
        start_tempo = math.log(args.startTempo, 10)
    start_tempo_norm = (start_tempo - means[1][0]) / stds[1][0]
    input_y = torch.zeros(1, 1, TOTAL_OUTPUT)
    # if args.trainTrill:
    #     input_y = torch.zeros(1, 1, output_size)
    # else:
    #     input_y = torch.zeros(1, 1, output_size - num_trill_param)
    # input_y[0,0,0] = start_tempo
    # # input_y[0,0,1] = 1
    # # input_y[0,0,2] = 64

    #
    input_y[0,0,0] = start_tempo_norm
    for i in range(1, TOTAL_OUTPUT - 1):
        input_y[0, 0, i] -= means[1][i]
        input_y[0, 0, i] /= stds[1][i]
    input_y = input_y.to(device)
    tempo_stats = [means[1][0], stds[1][0]]

    initial_z = [0] * NET_PARAM.encoder.size

    prediction = perform_xml(batch_x, input_y, edges, note_locations, tempo_stats, initial_z=initial_z)

    # outputs = outputs.view(-1, num_output)
    prediction = np.squeeze(np.asarray(prediction))
    # prediction = outputs.cpu().detach().numpy()
    for i in range(15):
        prediction[:, i] *= stds[1][i]
        prediction[:, i] += means[1][i]
    # print(prediction)
    # print(means, stds)
    output_features = []
    # for i in range(100):
    #     pred = prediction[i]
    #     print(pred[0:4])
    num_notes = len(xml_notes)
    for i in range(num_notes):
        pred = prediction[i]
        # feat = {'IOI_ratio': pred[0], 'articulation': pred[1], 'loudness': pred[2], 'xml_deviation': 0,
        feat = xml_matching.MusicFeature()
        feat.qpm = pred[0]
        feat.velocity = pred[1]
        feat.xml_deviation = pred[2]
        feat.articulation = pred[3]
        # feat.xml_deviation = 0
        feat.pedal_refresh_time = pred[4]
        feat.pedal_cut_time = pred[5]
        feat.pedal_at_start = pred[6]
        feat.pedal_at_end = pred[7]
        feat.soft_pedal = pred[8]
        feat.pedal_refresh = pred[9]
        feat.pedal_cut = pred[10]

        feat.beat_index = note_locations[i].beat
        feat.measure_index = note_locations[i].measure

        feat.trill_param = pred[11:16]
        feat.trill_param[0] = round(feat.trill_param[0]).astype(int)
        feat.trill_param[1] = (feat.trill_param[1])
        feat.trill_param[2] = (feat.trill_param[2])
        feat.trill_param[3] = (feat.trill_param[3])
        feat.trill_param[4] = round(feat.trill_param[4])

        if test_x[i][is_trill_index_score] == 1:
            print(feat.trill_param)
        #
        # feat.passed_second = pred[0]
        # feat.duration_second = pred[1]
        # feat.pedal_refresh_time = pred[3]
        # feat.pedal_cut_time = pred[4]
        # feat.pedal_at_start = pred[5]
        # feat.pedal_at_end = pred[6]
        # feat.soft_pedal = pred[7]
        # feat.pedal_refresh = pred[8]
        # feat.pedal_cut = pred[9]

        # feat = {'qpm': pred[0], 'articulation': pred[1], 'loudness': pred[2], 'xml_deviation': pred[3],
        #         'pedal_at_start': pred[6], 'pedal_at_end': pred[7], 'soft_pedal': pred[8],
        #         'pedal_refresh_time': pred[4], 'pedal_cut_time': pred[5], 'pedal_refresh': pred[9],
        #         'pedal_cut': pred[10]}
        output_features.append(feat)
    num_notes = len(xml_notes)
    performanceWorm.plot_performance_worm(output_features, path_name + 'perfWorm.png')

    # output_xml = xml_matching.apply_perform_features(xml_notes, output_features)
    output_xml = xml_matching.apply_tempo_perform_features(xml_doc, xml_notes, output_features, start_time= 1, predicted=True)
    # output_xml = xml_matching.apply_time_position_features(xml_notes, output_features, start_time=1)

    output_midi = xml_matching.xml_notes_to_midi(output_xml)

    xml_matching.save_midi_notes_as_piano_midi(output_midi, path_name + 'performed_by_nn.mid', bool_pedal=False, disklavier=True)



elif args.sessMode=='plot':
    if os.path.isfile(args.resume):
        print("=> loading checkpoint '{}'".format(args.resume))
        checkpoint = torch.load(args.resume)
        # args.start_epoch = checkpoint['epoch']
        best_valid_loss = checkpoint['best_valid_loss']
        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        print("=> loaded checkpoint '{}' (epoch {})"
              .format(args.resume, checkpoint['epoch']))
    else:
        print("=> no checkpoint found at '{}'".format(args.resume))


    with open(args.dataName + ".dat", "rb") as f:
        u = pickle._Unpickler(f)
        u.encoding = 'latin1'
        # p = u.load()
        # complete_xy = pickle.load(f)
        complete_xy = u.load()

    with open(args.dataName + "_stat.dat", "rb") as f:
        u = pickle._Unpickler(f)
        u.encoding = 'latin1'
        means, stds = u.load()

    perform_num = len(complete_xy)
    tempo_stats = [means[1][0], stds[1][0]]

    train_perf_num = int(perform_num * training_ratio)
    train_xy = complete_xy[:train_perf_num]
    test_xy = complete_xy[train_perf_num:]

    n_tuple = 0
    for xy_tuple in test_xy:
        n_tuple += 1
        train_x = xy_tuple[0]
        train_y = xy_tuple[1]
        prev_feature = xy_tuple[2]
        note_locations = xy_tuple[3]

        data_size = len(train_x)
        total_batch_num = int(math.ceil(data_size / (time_steps * batch_size)))
        batch_size=1
        for step in range(total_batch_num - 1):
            batch_start = step * batch_size * time_steps
            batch_end = (step + 1) * batch_size * time_steps
            batch_x = Variable(
                torch.Tensor(train_x[batch_start:batch_end]))
            batch_y = train_y[batch_start:batch_end]
            # print(batch_x.shape, batch_y.shape)
            # input_y = Variable(
            #     torch.Tensor(prev_feature[step * batch_size * time_steps:(step + 1) * batch_size * time_steps]))
            # input_y = torch.cat((zero_tensor, batch_y[0:batch_size * time_steps-1]), 0).view((batch_size, time_steps,num_output)).to(device)
            batch_x = batch_x.view((batch_size, time_steps, SCORE_INPUT)).to(device)
            # is_beat_batch = is_beat_list[batch_start:batch_end]
            # batch_y = batch_y.view((batch_size, time_steps, num_output)).to(device)
            # input_y = input_y.view((batch_size, time_steps, num_output)).to(device)

            # hidden = model.init_hidden(1)
            # final_hidden = model.init_final_layer(1)
            # outputs, hidden, final_hidden = model(batch_x, input_y, hidden, final_hidden)
            #
            if args.trainTrill:
                input_y = torch.zeros(1, 1, TOTAL_OUTPUT)
            else:
                input_y = torch.zeros(1, 1, TOTAL_OUTPUT - num_trill_param)

            input_y[0] = batch_y[0][0]
            input_y = input_y.view((1, 1, TOTAL_OUTPUT)).to(device)
            outputs = perform_xml(batch_x, input_y, note_locations, tempo_stats)
            outputs = torch.Tensor(outputs).view((1, -1, TOTAL_OUTPUT))

            outputs = outputs.cpu().detach().numpy()
            # batch_y = batch_y.cpu().detach().numpy()
            batch_y = np.asarray(batch_y).reshape((1, -1, TOTAL_OUTPUT))
            plt.figure(figsize=(10, 7))
            for i in range(4):
                plt.subplot(411+i)
                plt.plot(batch_y[0, :, i])
                plt.plot(outputs[0, :, i])
            plt.savefig('images/piece{:d},seg{:d}.png'.format(n_tuple, step))
            plt.close()
