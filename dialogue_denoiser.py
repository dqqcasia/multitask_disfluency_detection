# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Binary for training translation models and decoding from them.

Running this program without --decode will download the WMT corpus into
the directory specified as --data_dir and tokenize it in a very basic way,
and then start training a model saving checkpoints to --train_dir.

Running with --decode starts an interactive loop so you can see how
the current checkpoint translates English sentences into French.

See the following papers for more information on neural translation models.
 * http://arxiv.org/abs/1409.3215
 * http://arxiv.org/abs/1409.0473
 * http://arxiv.org/abs/1412.2007
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import logging
import shutil

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf
from copy_seq2seq import data_utils, seq2seq_model

tf.app.flags.DEFINE_float("learning_rate", 0.1, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99, "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0, "Clip gradients to this norm.")
tf.app.flags.DEFINE_float("word_dropout_prob", 0.0, "Word dropout probability during training")
tf.app.flags.DEFINE_float("early_stopping_threshold",
                          0.01,
                          "Loss decreasing less than this (relatively) will cause early stopping")
tf.app.flags.DEFINE_integer("batch_size", 16, "Batch size to use during training.")  # 8
tf.app.flags.DEFINE_integer("size", 64, "Size of each model layer.")  # 32
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("from_vocab_size", 100, "English vocabulary size.")  # 100
tf.app.flags.DEFINE_integer("to_vocab_size", 100, "French vocabulary size.")  # 100
tf.app.flags.DEFINE_string("data_dir", "/tmp", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "/tmp", "Training directory.")
tf.app.flags.DEFINE_string("from_train_data", None, "Training data.")
tf.app.flags.DEFINE_string("to_train_data", None, "Training data.")
tf.app.flags.DEFINE_string("from_dev_data", None, "Development data.")
tf.app.flags.DEFINE_string("to_dev_data", None, "Development data.")
tf.app.flags.DEFINE_string("from_test_data", None, "Testing data.")
tf.app.flags.DEFINE_string("to_test_data", None, "Testing data.")
tf.app.flags.DEFINE_integer("max_train_data_size",
                            0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint",
                            200,
                            "How many training steps to do per checkpoint.")  # 200
tf.app.flags.DEFINE_boolean("decode", False, "Set to True for interactive decoding.")
tf.app.flags.DEFINE_boolean("evaluate", False, "Set to True for evaluation.")
tf.app.flags.DEFINE_boolean("self_test", False, "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("use_fp16", False, "Train using fp16 instead of fp32.")
tf.app.flags.DEFINE_boolean("combined_vocabulary",
                            False,
                            "Using a combined encoder/decoder vocabulary")
tf.app.flags.DEFINE_integer("early_stopping_checkpoints",
                            10,
                            "Terminating training after this number of checkpoints of loss increase")
tf.app.flags.DEFINE_boolean("force_make_data",
                            False,
                            "Create datasets even if corresponding files exist")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
# See seq2seq_model.Seq2SeqModel for details of how they work.
_buckets = [(150, 100)]


def get_perplexity(in_loss):
    return math.exp(float(in_loss)) if in_loss < 300 else float("inf")


def read_data(encoder_input, decoder_input, max_size=None):
    """Read data from source and target files and put into buckets.

    Args:
        source_path: path to the files with token-ids for the source language.
        target_path: path to the file with token-ids for the target language;
            it must be aligned with the source file: n-th line contains the desired
            output for n-th line from the source_path.
        max_size: maximum number of lines to read, all other will be ignored;
            if 0 or None, data files will be read completely (no limit).

    Returns:
        data_set: a list of length len(_buckets); data_set[n] contains a list of
            (source, target) pairs read from the provided data files that fit
            into the n-th bucket, i.e., such that len(source) < _buckets[n][0] and
            len(target) < _buckets[n][1]; source and target are lists of token-ids.
    """
    data_set = [[] for _ in _buckets]
    with tf.gfile.GFile(encoder_input, mode="r") as encoder_input_file, \
         tf.gfile.GFile(decoder_input, mode="r") as decoder_input_file:
        encoder_input, decoder_input = encoder_input_file.readline(), decoder_input_file.readline()
        counter = 0
        while encoder_input and decoder_input and (not max_size or counter < max_size):
            counter += 1
            if counter % 100 == 0:
                print("  reading data line %d" % counter)
                sys.stdout.flush()
            encoder_ids = [int(x) for x in encoder_input.split()]
            decoder_ids = [int(x) for x in decoder_input.split()]
            decoder_ids.append(data_utils.EOS_ID)

            for bucket_id, (source_size, target_size) in enumerate(_buckets):
                if len(encoder_ids) < source_size and len(decoder_ids) < target_size:
                    data_set[bucket_id].append([encoder_ids, decoder_ids])
                    break
            encoder_input, decoder_input = (encoder_input_file.readline(),
                                            decoder_input_file.readline())
    return data_set


def create_model(session, from_vocab_size, to_vocab_size, forward_only, force_create_fresh=False):
    """Create translation model and initialize or load parameters in session."""
    dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
    model = seq2seq_model.Seq2SeqModel(from_vocab_size,
                                       to_vocab_size,
                                       _buckets,
                                       FLAGS.size,
                                       FLAGS.num_layers,
                                       FLAGS.max_gradient_norm,
                                       FLAGS.batch_size,
                                       FLAGS.learning_rate,
                                       FLAGS.learning_rate_decay_factor,
                                       forward_only=forward_only,
                                       dtype=dtype)
    if force_create_fresh:
        if os.path.exists(FLAGS.train_dir):
            shutil.rmtree(FLAGS.train_dir)
            os.makedirs(FLAGS.train_dir)
    ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
    if ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
        print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
        model.saver.restore(session, ckpt.model_checkpoint_path)
    else:
        print("Created model with fresh parameters.")
        session.run(tf.global_variables_initializer())
    return model


def train():
    from_train_data = FLAGS.from_train_data
    to_train_data = FLAGS.to_train_data
    from_dev_data = FLAGS.from_dev_data
    to_dev_data = FLAGS.to_dev_data
    from_test_data = FLAGS.from_test_data
    to_test_data = FLAGS.to_test_data

    train_data, dev_data, test_data, _, _ = \
        data_utils.prepare_data(FLAGS.data_dir,
                                from_train_data,
                                to_train_data,
                                from_dev_data,
                                to_dev_data,
                                from_test_data,
                                to_test_data,
                                FLAGS.from_vocab_size,
                                FLAGS.to_vocab_size,
                                combined_vocabulary=FLAGS.combined_vocabulary,
                                force=FLAGS.force_make_data)
    from_train, to_train = train_data
    from_dev, to_dev = dev_data
    from_test, to_test = test_data

    encoder_size, decoder_size = _buckets[-1]
    train_tokenized = [(from_line + [data_utils.PAD_ID] * (encoder_size - len(from_line)), to_line)
                       for from_line, to_line in zip(data_utils.tokenize_data(from_train_data),
                                                     data_utils.tokenize_data(to_train_data))]
    dev_tokenized = [(from_line + [data_utils.PAD_ID] * (encoder_size - len(from_line)), to_line)
                     for from_line, to_line in zip(data_utils.tokenize_data(from_dev_data),
                                                   data_utils.tokenize_data(to_dev_data))]
    test_tokenized = [(from_line + [data_utils.PAD_ID] * (encoder_size - len(from_line)), to_line)
                      for from_line, to_line in zip(data_utils.tokenize_data(from_test_data),
                                                    data_utils.tokenize_data(to_test_data))]

    enc_vocab_path = os.path.join(FLAGS.data_dir, "vocab.from")
    dec_vocab_path = os.path.join(FLAGS.data_dir, "vocab.to")
    enc_vocab, rev_enc_vocab = data_utils.initialize_vocabulary(enc_vocab_path)
    dec_vocab, rev_dec_vocab = data_utils.initialize_vocabulary(dec_vocab_path)
    with tf.Session() as sess:
        # Create model.
        print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
        model = create_model(sess,
                             len(enc_vocab),
                             len(dec_vocab),
                             False,
                             force_create_fresh=FLAGS.force_make_data)

        # Read data into buckets and compute their sizes.
        print("Reading train/dev/test data (limit: %d)." % FLAGS.max_train_data_size)
        train_set = read_data(from_train, to_train, FLAGS.max_train_data_size)
        dev_set = read_data(from_dev, to_dev)
        test_set = read_data(from_test, to_test)

        train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
        train_total_size = float(sum(train_bucket_sizes))

        # A bucket scale is a list of increasing numbers from 0 to 1 that we'll use
        # to select a bucket. Length of [scale[i], scale[i+1]] is proportional to
        # the size if i-th training bucket, as used later.
        train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size
                               for i in xrange(len(train_bucket_sizes))]

        # This is the training loop.
        loss = 0.0
        current_step = 0
        best_train_loss, best_dev_loss, best_test_loss = None, None, None
        best_loss_step = 0
        suboptimal_loss_steps = 0
        previous_losses = []
        checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")

        while True:
            # Choose a bucket according to data distribution. We pick a random number
            # in [0, 1] and use the corresponding interval in train_buckets_scale.
            random_number_01 = np.random.random_sample()
            bucket_id = min([i for i in xrange(len(train_buckets_scale))
                             if train_buckets_scale[i] > random_number_01])

            # Get a batch and make a step.
            encoder_inputs, decoder_inputs, decoder_targets, target_weights = \
                model.get_batch(train_set, bucket_id, word_dropout_prob=FLAGS.word_dropout_prob)
            _, step_loss, _ = model.step(sess,
                                         encoder_inputs,
                                         decoder_inputs,
                                         decoder_targets,
                                         target_weights,
                                         bucket_id,
                                         False)
            loss += step_loss / FLAGS.steps_per_checkpoint
            current_step += 1

            # Once in a while, we save checkpoint, print statistics, and run evals.
            if current_step % FLAGS.steps_per_checkpoint == 0:
                # Print statistics for the previous epoch.
                perplexity = get_perplexity(loss)
                print("global step %d learning rate %.4f loss %.2f perplexity "
                      "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                                loss, perplexity))
                loss = 0.0
                # Decrease learning rate if no improvement was seen over last 3 times.
                if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
                    sess.run(model.learning_rate_decay_op)
                previous_losses.append(loss)

                train_loss, train_perplexity, train_accuracy = eval_model(sess,
                                                                          model,
                                                                          rev_dec_vocab,
                                                                          train_set,
                                                                          train_tokenized)
                dev_loss, dev_perplexity, dev_accuracy = eval_model(sess,
                                                                    model,
                                                                    rev_dec_vocab,
                                                                    dev_set,
                                                                    dev_tokenized)
                test_loss, test_perplexity, test_accuracy = eval_model(sess,
                                                                       model,
                                                                       rev_dec_vocab,
                                                                       test_set,
                                                                       test_tokenized)
                print("  train: loss %.2f perplexity %.2f per-utterance accuracy %.2f"
                      % (train_loss, train_perplexity, train_accuracy))
                print("  dev: loss %.2f perplexity %.2f per-utterance accuracy %.2f"
                      % (dev_loss, dev_perplexity, dev_accuracy))
                print("  test: loss %.2f perplexity %.2f per-utterance accuracy %.2f"
                      % (test_loss, test_perplexity, test_accuracy))
                sys.stdout.flush()

                if best_dev_loss is None or FLAGS.early_stopping_threshold < (best_dev_loss - dev_loss) / (best_dev_loss + 1e-12):
                  suboptimal_loss_steps = 0
                  best_train_loss, best_dev_loss, best_test_loss = train_loss, dev_loss, test_loss
                  best_loss_step = model.global_step
                  # Save checkpoint and zero timer and loss.
                  model.saver.save(sess, checkpoint_path, global_step=model.global_step)
                else:
                  suboptimal_loss_steps += 1
                  if FLAGS.early_stopping_checkpoints <= suboptimal_loss_steps:
                    print("Early stopping after %d checkpoints" % FLAGS.early_stopping_checkpoints)
                    break
        print("Best loss achieved: %.2f (train) %.2f (*dev) %.2f (test)"
              % (best_train_loss, best_dev_loss, best_test_loss))


def eval_model(in_session, in_model, in_rev_dec_vocab, dataset, in_dataset_tokenized):
    original_batch_size = in_model.batch_size
    in_model.batch_size = 64

    results = []
    losses = []
    for bucket_id in xrange(len(dataset)):
        bucket_data = dataset[bucket_id]
        for index in xrange(0, len(bucket_data), in_model.batch_size):
            sequences_tokenized = in_dataset_tokenized[index: index + in_model.batch_size]

            enc_in, dec_in, dec_tgt, target_weights = \
                in_model.get_batch({bucket_id: bucket_data}, bucket_id, start_index=index)
            # Get output logits for the sentence.
            _, loss, output_logits = in_model.step(in_session,
                                                enc_in,
                                                dec_in,
                                                dec_tgt,
                                                target_weights,
                                                bucket_id,
                                                True)
            losses.append(loss)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.
            outputs = [[] for _ in xrange(len(output_logits[0]))]
            for output_tensor in output_logits:
                for token_index, token in enumerate(output_tensor):
                    outputs[token_index].append(token)
            for output_sequence, (encoder_tokens, decoder_tokens) in zip(outputs,
                                                                         sequences_tokenized):
                final_output = get_decoded_sequence(output_sequence,
                                                    in_rev_dec_vocab,
                                                    encoder_tokens)
                results.append(int(decoder_tokens == final_output))
            # print('Gold: ', ' '.join(map(str, decoder_inputs)))
            # print('Pred: ', ' '.join(map(str, outputs)))
            # print("Processed {} out of {} data points".format(index, len(bucket_data)))
    in_model.batch_size = original_batch_size
    loss = np.mean(losses)
    perplexity = get_perplexity(loss)
    per_utterance_accuracy = sum(results) / float(len(results))
    return loss, perplexity, per_utterance_accuracy


def get_decoded_sequence(in_decoder_argmax, in_rev_vocab, in_encoder_sequence):
    sequence = in_decoder_argmax[:]
    if data_utils.EOS_ID in sequence:
        sequence = sequence[:sequence.index(data_utils.EOS_ID)]
    result = [in_rev_vocab[token_id] \
                  if token_id < len(in_rev_vocab) \
                  else in_encoder_sequence[token_id - len(in_rev_vocab)]
              for token_id in sequence]
    return result


def decode():
    with tf.Session() as sess:
        # Load vocabularies.
        en_vocab_path = os.path.join(FLAGS.data_dir, 'vocab.from')
        fr_vocab_path = os.path.join(FLAGS.data_dir, 'vocab.to')
        en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
        fr_vocab, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)

        # Create model and load parameters.
        model = create_model(sess, len(en_vocab), len(fr_vocab), True)
        model.batch_size = 1  # We decode one sentence at a time.

        # Decode from standard input.
        sys.stdout.write('> ')
        sys.stdout.flush()
        sentence = sys.stdin.readline()
        while sentence:
            input_tokens = data_utils.basic_tokenizer(tf.compat.as_bytes(sentence))
            # Get token-ids for the input sentence.
            token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sentence), en_vocab)
            # Which bucket does it belong to?
            bucket_id = len(_buckets) - 1
            for i, bucket in enumerate(_buckets):
                if bucket[0] >= len(token_ids):
                    bucket_id = i
                    break
            else:
                logging.warning('Sentence truncated: %s', sentence)

            # Get a 1-element batch to feed the sentence to the model.
            encoder_inputs, decoder_inputs, decoder_targets, target_weights = \
                model.get_batch({bucket_id: [(token_ids, [])]}, bucket_id)
            # Get output logits for the sentence.
            _, _, output_logits = model.step(sess,
                                             encoder_inputs,
                                             decoder_inputs,
                                             decoder_targets,
                                             target_weights,
                                             bucket_id,
                                             True)
            # This is a greedy decoder - outputs are just argmaxes of output_logits.

            outputs = [logit[0] for logit in output_logits]
            final_outputs = get_decoded_sequence(outputs, rev_fr_vocab, input_tokens)
            print(" ".join(final_outputs))
            print("> ", end="")
            sys.stdout.flush()
            sentence = sys.stdin.readline()


def evaluate():
    with tf.Session() as sess:
        # Load vocabularies.
        en_vocab_path = os.path.join(FLAGS.data_dir, 'vocab.from')
        fr_vocab_path = os.path.join(FLAGS.data_dir, 'vocab.to')
        from_dev_path = FLAGS.from_dev_data
        to_dev_path = FLAGS.to_dev_data
        from_dev, to_dev = data_utils.make_dataset(from_dev_path,
                                                   to_dev_path,
                                                   en_vocab_path,
                                                   fr_vocab_path,
                                                   tokenizer=None,
                                                   force=FLAGS.force_make_data)
        en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
        fr_vocab, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)
        model = create_model(sess, len(en_vocab), len(fr_vocab), forward_only=True)

        encoder_size, decoder_size = _buckets[-1]
        dev_set = read_data(from_dev, to_dev)
        dev_tokenized = [(from_line + [data_utils.PAD_ID] * (encoder_size - len(from_line)), to_line)
                         for from_line, to_line in zip(data_utils.tokenize_data(from_dev_path),
                                                       data_utils.tokenize_data(to_dev_path))]
        loss, perplexity, accuracy = eval_model(sess, model, rev_fr_vocab, dev_set, dev_tokenized)
        print("  test: loss %.2f perplexity %.2f per-utterance accuracy %.2f" %
              (loss, perplexity, accuracy))


def self_test():
    """Test the translation model."""
    with tf.Session() as sess:
        print("Self-test for neural translation model.")
        # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
        model = seq2seq_model.Seq2SeqModel(10,
                                           10,
                                           [(3, 3), (6, 6)],
                                           32,
                                           2,
                                           5.0,
                                           32,
                                           0.3,
                                           0.99,
                                           num_samples=8)
        sess.run(tf.global_variables_initializer())

        # Fake data set for both the (3, 3) and (6, 6) bucket.
        data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
                    [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
        for _ in xrange(5):  # Train the fake model for 5 steps.
            bucket_id = random.choice([0, 1])
            encoder_inputs, decoder_inputs, target_weights = model.get_batch(data_set, bucket_id)
            model.step(sess, encoder_inputs, decoder_inputs, target_weights, bucket_id, False)


def main(_):
    if FLAGS.self_test:
        self_test()
    elif FLAGS.decode:
        decode()
    elif FLAGS.evaluate:
        evaluate()
    else:
        train()


if __name__ == "__main__":
    tf.app.run()
