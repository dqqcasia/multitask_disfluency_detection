import json
import random
from functools import partial, update_wrapper
from operator import itemgetter
import os
from collections import defaultdict

import keras
from keras.layers import (TimeDistributed,
                          Embedding,
                          Conv1D,
                          Conv2D,
                          MaxPool1D,
                          MaxPool2D,
                          Flatten,
                          LSTM,
                          Reshape)
import tensorflow as tf
import numpy as np
from sklearn.utils.class_weight import compute_class_weight

from data_utils import vectorize_sequences, pad_sequences, PAD_ID
from deep_disfluency_utils import make_tag_mapping
from metrics import DisfluencyDetectionF1Score

random.seed(273)
np.random.seed(273)
tf.set_random_seed(273)

VOCABULARY_SIZE = 15000
MAX_INPUT_LENGTH = 80
MEAN_WORD_LENGTH = 8
CONTEXT_LENGTH = 3
MAX_CHAR_INPUT_LENGTH = CONTEXT_LENGTH * (MEAN_WORD_LENGTH + 1)

MODEL_NAME = 'model.h5'
VOCABULARY_NAME = 'vocab.json'
CHAR_VOCABULARY_NAME = 'char_vocab.json'
LABEL_VOCABULARY_NAME = 'label_vocab.json'


def wrapped_partial(func, *args, **kwargs):
    partial_func = partial(func, *args, **kwargs)
    update_wrapper(partial_func, func)
    return partial_func


def get_sample_weight(in_labels):
    labels_filtered = filter(lambda x: x != 0, in_labels.flatten())
    class_weight = compute_class_weight('balanced', np.unique(labels_filtered), labels_filtered)
    class_weight_map = {class_id: weight for class_id, weight in zip(np.unique(labels_filtered), class_weight)}
    class_weight_map[0] = 0.0

    sample_weight = np.vectorize(class_weight_map.get)(in_labels)
    return sample_weight


def make_dataset(in_data_points, in_vocab, in_char_vocab, in_label_vocab):
    utterances_tokenized, tags = (map(itemgetter(0), in_data_points),
                                  map(itemgetter(1), in_data_points))
    tokens_vectorized = vectorize_sequences(utterances_tokenized, in_vocab)
    tokens_padded = pad_sequences(tokens_vectorized, MAX_INPUT_LENGTH)
    chars_vectorized = []
    for utterance_tokenized in utterances_tokenized:
        contexts = [' '.join(utterance_tokenized[max(i - CONTEXT_LENGTH + 1, 0): i + 1])
                    for i in xrange(len(utterance_tokenized))]
        contexts_vectorized = vectorize_sequences(contexts, in_char_vocab)
        contexts_padded = pad_sequences(contexts_vectorized, MAX_CHAR_INPUT_LENGTH)
        chars_vectorized += [contexts_padded]
    chars_vectorized = pad_sequences(chars_vectorized, MAX_INPUT_LENGTH)
    labels = vectorize_sequences(map(itemgetter(1), in_data_points), in_label_vocab)
    labels_padded = pad_sequences(labels, MAX_INPUT_LENGTH)
    sample_weight = get_sample_weight(labels_padded)
    y = keras.utils.to_categorical(labels_padded, num_classes=len(in_label_vocab))
 
    return [tokens_padded, chars_vectorized], y, sample_weight


def char_cnn_module(in_char_input, in_vocab_size, in_emb_size):
    """
        Zhang and LeCun, 2015
    """
    model = TimeDistributed(Embedding(in_vocab_size, in_emb_size, mask_zero=True))(in_char_input)
    model = TimeDistributed(Reshape((27, in_emb_size, 1)))(model)
    model = TimeDistributed(Conv2D(32, (3, 3), activation='relu', name='chars'))(model)
    model = TimeDistributed(MaxPool2D((3, 3)))(model)

    model = TimeDistributed(Conv2D(32, (3, 3), activation='relu'))(model)
    model = TimeDistributed(MaxPool2D((1, 1)))(model)

    model = TimeDistributed(Conv2D(32, (3, 3), activation='relu'))(model)
    model = TimeDistributed(MaxPool2D((1, 1)))(model)

    model = TimeDistributed(Flatten())(model)

    return model


def char_cnn_1d_module(in_char_input, in_vocab_size, in_emb_size):
    """
        Zhang and LeCun, 2015
    """
    model = TimeDistributed(Embedding(in_vocab_size, in_emb_size, mask_zero=True))(in_char_input)
    # model = TimeDistributed(Reshape((27, in_emb_size, 1)))(model)
    model = TimeDistributed(Conv1D(32, 3, activation='relu', name='chars'))(model)
    model = TimeDistributed(MaxPool1D(3))(model)

    model = TimeDistributed(Conv1D(32, 3, activation='relu'))(model)
    model = TimeDistributed(MaxPool1D(1))(model)

    model = TimeDistributed(Conv1D(32, 3, activation='relu'))(model)
    model = TimeDistributed(MaxPool1D(1))(model)

    model = TimeDistributed(Flatten())(model)

    return model


def rnn_module(in_word_input, in_vocab_size, in_cell_size):
    embedding = Embedding(in_vocab_size, in_cell_size, mask_zero=True)(in_word_input)
    lstm = LSTM(in_cell_size, return_sequences=True)(embedding)
    return lstm


def create_model(in_vocab_size,
                 in_char_vocab_size,
                 in_cell_size,
                 in_char_cell_size,
                 in_max_input_length,
                 in_max_char_input_length,
                 in_classes_number,
                 lr):
    word_input = keras.layers.Input(shape=(in_max_input_length,))
    # char_input = keras.layers.Input(shape=(in_max_input_length, in_max_char_input_length))
    rnn = rnn_module(word_input, in_vocab_size, in_cell_size)
    # char_cnn = char_cnn_module(char_input, in_char_vocab_size, in_char_cell_size)

    rnn_cnn_combined = rnn # keras.layers.Concatenate()([rnn, char_cnn])
    output = keras.layers.TimeDistributed(keras.layers.Dense(128, activation='relu'))(rnn_cnn_combined)
    # output = keras.layers.Dropout(0.5)(output)
    # output = keras.layers.Dense(128, activation='relu')(output)
    # output = keras.layers.Dropout(0.5)(output)
    output = keras.layers.TimeDistributed(keras.layers.Dense(in_classes_number,
                                                             activation='softmax',
                                                             name='labels'))(output)
    model = keras.Model(inputs=[word_input], outputs=[output])

    opt = keras.optimizers.Adam(lr=lr, clipnorm=5.0)
    model.compile(optimizer=opt,
                  loss='categorical_crossentropy',
                  metrics=['accuracy'],
                  sample_weight_mode='temporal')
    return model


def batch_generator(data, labels, weights, batch_size):
    """Generator used by `keras.models.Sequential.fit_generator` to yield batches
    of pairs.
    Such a generator is required by the parallel nature of the aforementioned
    Keras function. It can theoretically feed batches of pairs indefinitely
    (looping over the dataset). Ideally, it would be called so that an epoch ends
    exactly with the last batch of the dataset.
    """

    data_idx = range(labels.shape[0])
    while True:
        batch_idx = np.random.choice(data_idx, size=batch_size)
        batch = ([np.take(feature, batch_idx, axis=0) for feature in data],
                 np.take(labels, batch_idx, axis=0),
                 np.take(weights, batch_idx, axis=0))
        yield batch


def train(in_model,
          train_data,
          dev_data,
          test_data,
          in_checkpoint_filepath,
          label_vocab,
          epochs=100,
          batch_size=32,
          steps_per_epoch=1000,
          **kwargs):
    X_train, y_train, weights_train = train_data
    X_dev, y_dev, weights_dev = dev_data
    X_test, y_test, weights_test = test_data

    batch_gen = batch_generator(X_train, y_train, weights_train, batch_size)
    in_model.fit_generator(generator=batch_gen,
                           epochs=epochs,
                           steps_per_epoch=steps_per_epoch,
                           validation_data=(X_dev, y_dev, weights_dev),
                           callbacks=[keras.callbacks.ModelCheckpoint(in_checkpoint_filepath,
                                                                      monitor='val_loss',
                                                                      verbose=1,
                                                                      save_best_only=True,
                                                                      save_weights_only=False,
                                                                      mode='auto',
                                                                      period=1),
                                      keras.callbacks.EarlyStopping(monitor='val_loss',
                                                                    min_delta=0,
                                                                    patience=10,
                                                                    verbose=1,
                                                                    mode='auto'),
                                      keras.callbacks.ReduceLROnPlateau(monitor='val_loss',
                                                                        factor=0.2,
                                                                        patience=5,
                                                                        min_lr=0.001),
                                      DisfluencyDetectionF1Score(make_tag_mapping(label_vocab, mode=None))])
    test_loss = in_model.evaluate(x=X_test, y=y_test)
    print 'Evaluation results on testset'
    print 'Metrics: ', test_loss


def predict(in_model, X):
    model_out = in_model.predict(X)
    return np.argmax(model_out, axis=-1)


def denoise_line(in_line, in_model, in_vocab, in_char_vocab, in_rev_label_vocab):
    tokens = [in_line.lower().split()]
    tokens_vectorized = vectorize_sequences(tokens, in_vocab)
    chars_vectorized = []
    for utterance_tokenized in tokens:
        contexts = [' '.join(utterance_tokenized[max(i - CONTEXT_LENGTH + 1, 0): i + 1])
                    for i in xrange(len(utterance_tokenized))]
        contexts_vectorized = vectorize_sequences(contexts, in_char_vocab)
        chars_vectorized += [contexts_vectorized]
    chars_vectorized = pad_sequences(chars_vectorized, MAX_INPUT_LENGTH)

    predicted = predict(in_model, tokens_vectorized)[0]
    result_tokens = map(lambda x: in_rev_label_vocab[x], predicted[:len(tokens[0])])
    return ' '.join(result_tokens)


def evaluate(in_model, X, y):
    y_pred = np.argmax(in_model.predict(X), axis=-1)
    y_gold = np.argmax(y, axis=-1)
    return sum([int(np.array_equal(y_pred_i, y_gold_i))
                for y_pred_i, y_gold_i in zip(y_pred, y_gold)]) / float(y.shape[0])


def create_simple_model(in_vocab_size,
                        in_char_vocab_size,
                        in_cell_size,
                        in_char_cell_size,
                        in_max_input_length,
                        in_max_char_input_length,
                        in_classes_number,
                        lr):
    word_input = keras.layers.Input(shape=(in_max_input_length,))
    #char_input = keras.layers.Input(shape=(in_max_input_length, in_max_char_input_length))
    rnn = rnn_module(word_input, in_vocab_size, in_cell_size)
    #char_cnn = char_cnn_module(char_input, in_char_vocab_size, in_char_cell_size)

    rnn_cnn_combined = rnn #keras.layers.Concatenate()([rnn, char_cnn])
    output = keras.layers.Dense(256, activation='relu')(rnn_cnn_combined)
    output = keras.layers.Dropout(0.5)(output)
    # output = keras.layers.Dense(128, activation='relu')(output)
    # output = keras.layers.Dropout(0.5)(output)
    output = keras.layers.Dense(in_classes_number,
                                activation='softmax',
                                name='labels')(output)
    model = keras.Model(inputs=[word_input], outputs=[output])

    opt = keras.optimizers.Adam(lr=lr, clipnorm=10.0)
    model.compile(optimizer=opt,
                  loss='categorical_crossentropy',
                  metrics=['accuracy'],
                  sample_weight_mode='temporal')
    return model


def load(in_model_folder):
    with open(os.path.join(in_model_folder, VOCABULARY_NAME)) as vocab_in:
        vocab = json.load(vocab_in)
    with open(os.path.join(in_model_folder, CHAR_VOCABULARY_NAME)) as char_vocab_in:
        char_vocab = json.load(char_vocab_in)
    with open(os.path.join(in_model_folder, LABEL_VOCABULARY_NAME)) as label_vocab_in:
        label_vocab = json.load(label_vocab_in)
    model = keras.models.load_model(os.path.join(in_model_folder, MODEL_NAME))
    return model, vocab, char_vocab, label_vocab


def save(in_model, in_vocab, in_char_vocab, in_label_vocab, in_model_folder, save_model=False):
    if not os.path.exists(in_model_folder):
        os.makedirs(in_model_folder)
    if save_model:
        in_model.save(os.path.join(in_model_folder, MODEL_NAME))
    with open(os.path.join(in_model_folder, VOCABULARY_NAME), 'w') as vocab_out:
        json.dump(in_vocab, vocab_out)
    with open(os.path.join(in_model_folder, CHAR_VOCABULARY_NAME), 'w') as char_vocab_out:
        json.dump(in_char_vocab, char_vocab_out)
    with open(os.path.join(in_model_folder, LABEL_VOCABULARY_NAME), 'w') as label_vocab_out:
        json.dump(in_label_vocab, label_vocab_out)
