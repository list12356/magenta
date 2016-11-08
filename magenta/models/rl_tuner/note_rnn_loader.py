"""Defines a class and operations for the MelodyRNN model.

Note RNN Loader allows a basic melody prediction LSTM RNN model to be loaded 
from a checkpoint file, primed, and used to predict next notes.

This class can be used as the q_network and target_q_network for the RLTuner
class.

The graph structure of this model is similar to basic_rnn, but more flexible.
It allows you to either train it with data from a queue, or just 'call' it to
produce the next action.

It also provides the ability to add the model's graph to an existing graph as a
subcomponent, and then load variables from a checkpoint file into only that
piece of the overall graph.

These functions are necessary for use with the RL Tuner class.
"""

import os

import numpy as np
import tensorflow as tf

from magenta.music import melodies_lib
from magenta.music import midi_io
from magenta.music import sequences_lib
from magenta.common import sequence_example_lib

import note_rnn_encoder_decoder
import rl_tuner_ops


DEFAULT_BPM = 80.0


class NoteRNNLoader(object):
  """Builds graph for a Note RNN and instantiates weights from a checkpoint.

  Loads weights from a previously saved checkpoint file corresponding to a pre-
  trained basic_rnn model. Has functions that allow it to be primed with a MIDI
  melody, and allow it to be called to produce its predictions for the next
  note in a sequence.

  Used as part of the RLTuner class.
  """

  def __init__(self, graph, scope, experiment_dir, midi_primer=None,
               training_file_list=None, hparams=None,
               backup_checkpoint_file=None, softmax_within_graph=True,
               checkpoint_scope='rnn_model', bpm=DEFAULT_BPM):
    """Initialize by building the graph and loading a previous checkpoint.

    Args:
      graph: A tensorflow graph where the MelodyRNN's graph will be added.
      scope: The tensorflow scope where this network will be saved.
      experiment_dir: Path to the directory where the checkpoint file is saved.
      midi_primer: Path to a single midi file that can be used to prime the
        model.
      training_file_list: List of paths to tfrecord files containing melody 
        training data.
      hparams: A tf_lib.HParams object. Must match the hparams used to create the
        checkpoint file.
      backup_checkpoint_file: Path to a backup checkpoint file to be used if
        none can be found in the experiment_dir
      softmax_within_graph: If True, then when the network is called, it will
        output softmax probabilities for the next note. if False, it will output
        logits only. Used to control whether MelodyQ network is reinforcing
        softmax probabilities or logits.
      checkpoint_scope: The scope in lstm which the model was originally defined
        when it was first trained.
      bpm: Beats per minute to use for the compositions.
    """
    self.graph = graph
    self.session = None
    self.scope = scope
    self.batch_size = 1
    self.midi_primer = midi_primer
    self.softmax_within_graph = softmax_within_graph
    self.checkpoint_scope = checkpoint_scope
    self.training_file_list = training_file_list
    self.bpm = bpm

    if hparams is not None:
      tf.logging.info('Using custom hparams')
      self.hparams = hparams
    else:
      tf.logging.info('Empty hparams string. Using defaults')
      self.hparams = rl_tuner_ops.default_hparams()

    self.backup_checkpoint_file = backup_checkpoint_file

    self.build_graph()
    self.state_value = self.get_zero_state()

    if midi_primer is not None:
      self.load_primer()

    self.variable_names = rl_tuner_ops.get_variable_names(self.graph, self.scope)

    self.transpose_amount = 0

  def get_zero_state(self):
    """Gets an initial state of zeros of the appropriate size.

    Required size is based on the model's internal RNN cell.

    Returns:
      A matrix of batch_size x cell size zeros.
    """
    return np.zeros((self.batch_size, self.cell.state_size))

  def restore_initialize_prime(self, session):
    """Saves the session, restores variables from checkpoint, primes model.

    Model is primed with its default midi file.

    Args:
      session: A tensorflow session.
    """
    self.session = session
    self.restore_vars_from_checkpoint(self.checkpoint_dir)
    self.prime_model()

  def initialize_and_restore(self, session):
    """Saves the session, restores variables from checkpoint.

    Args:
      session: A tensorflow session.
    """
    self.session = session
    self.restore_vars_from_checkpoint(self.checkpoint_dir)

  def initialize_new(self, session=None):
    """Saves the session, initializes all variables to random values.

    Args:
      session: A tensorflow session.
    """
    with self.graph.as_default():
      if session is None:
        self.session = tf.Session(graph=self.graph)
      else:
        self.session = session
      self.session.run(tf.initialize_all_variables())

  def get_variable_name_dict(self):
    """Constructs a dict mapping the checkpoint variables to those in new graph.

    Returns:
      A dict mapping variable names in the checkpoint to variables in the graph.
    """
    var_dict = dict()
    for var in self.variables():
      inner_name = rl_tuner_ops.get_inner_scope(var.name)
      inner_name = rl_tuner_ops.trim_variable_postfixes(inner_name)
      var_dict[self.checkpoint_scope + '/' + inner_name] = var
    return var_dict

  def build_graph(self):
    """Constructs the portion of the graph that belongs to this model."""

    tf.logging.info('Initializing melody RNN graph for scope %s', self.scope)

    with self.graph.as_default():
      with tf.device(lambda op: ''):
        with tf.variable_scope(self.scope):
          # Make an LSTM cell with the number and size of layers specified in
          # hparams.
          self.cell = rl_tuner_ops.make_cell(self.hparams)

          # Shape of melody_sequence is batch size, melody length, number of
          # output note actions.
          self.melody_sequence = tf.placeholder(tf.float32,
                                                [None, None,
                                                 self.hparams.one_hot_length],
                                                name='melody_sequence')
          self.lengths = tf.placeholder(tf.int32, [None], name='lengths')
          self.initial_state = tf.placeholder(tf.float32,
                                              [None, self.cell.state_size],
                                              name='initial_state')

          if self.training_file_list is not None:
            # Set up a tf queue to read melodies from the training data tfrecord
            (self.train_sequence,
             self.train_labels,
             self.train_lengths) = sequence_example_lib.get_padded_batch(
                 self.training_file_list, self.hparams.batch_size, self.hparams.one_hot_length)

          # Closure function is used so that this part of the graph can be
          # re-run in multiple places, such as __call__.
          def run_network_on_melody(m_seq,
                                    lens,
                                    initial_state,
                                    swap_memory=True,
                                    parallel_iterations=1):
            """Internal function that defines the RNN network structure.

            Args:
              m_seq: A batch of melody sequences of one-hot notes.
              lens: Lengths of the melody_sequences.
              initial_state: Vector representing the initial state of the RNN.
              swap_memory: Uses more memory and is faster.
              parallel_iterations: Argument to tf.nn.dynamic_rnn.
            Returns:
              Output of network (either softmax or logits) and RNN state.
            """
            outputs, final_state = tf.nn.dynamic_rnn(
                self.cell,
                m_seq,
                sequence_length=lens,
                initial_state=initial_state,
                swap_memory=swap_memory,
                parallel_iterations=parallel_iterations)

            outputs_flat = tf.reshape(outputs,
                                      [-1, self.hparams.rnn_layer_sizes[-1]])
            logits_flat = tf.contrib.layers.legacy_linear(
                outputs_flat, self.hparams.one_hot_length)
            if self.softmax_within_graph:
              softmax = tf.nn.softmax(logits_flat)
              return softmax, final_state
            else:
              return logits_flat, final_state

          if self.softmax_within_graph:
            (self.softmax, self.state_tensor) = run_network_on_melody(
                self.melody_sequence, self.lengths, self.initial_state)
          else:
            (self.logits, self.state_tensor) = run_network_on_melody(
                self.melody_sequence, self.lengths, self.initial_state)
            self.softmax = tf.nn.softmax(self.logits)

          self.run_network_on_melody = run_network_on_melody

        if self.training_file_list is not None:
          # Does not recreate the model architecture but rather uses it to feed
          # data from the training queue through the model.
          with tf.variable_scope(self.scope, reuse=True):
            zero_state = self.cell.zero_state(
                batch_size=self.hparams.batch_size, dtype=tf.float32)

            if self.softmax_within_graph:
              (self.train_softmax, self.train_state) = run_network_on_melody(
                  self.train_sequence, self.train_lengths, zero_state)
            else:
              (self.train_logits, self.train_state) = run_network_on_melody(
                  self.train_sequence, self.train_lengths, zero_state)
              self.train_softmax = tf.nn.softmax(self.train_logits)

  def restore_vars_from_checkpoint(self, checkpoint_dir):
    """Loads model weights from a saved checkpoint.

    Args:
      checkpoint_dir: Directory which contains a saved checkpoint of the
        model.
    """
    tf.logging.info('Restoring variables from checkpoint')

    var_dict = self.get_variable_name_dict()
    with self.graph.as_default():
      saver = tf.train.Saver(var_list=var_dict)

    tf.logging.info('Checkpoint dir: %s', checkpoint_dir)
    print "Note RNN checkpoint dir", checkpoint_dir
    checkpoint_file = tf.train.latest_checkpoint(checkpoint_dir)
    if checkpoint_file is None:
      print "can't find checkpoint file, using backup, which is", self.backup_checkpoint_file
      checkpoint_file = self.backup_checkpoint_file
    tf.logging.info('Checkpoint file: %s', checkpoint_file)
    print "Note RNN checkpoint file", checkpoint_file

    saver.restore(self.session, checkpoint_file)

  def load_primer(self):
    """Loads default MIDI primer file.

    Also assigns the bpm and steps per bar of this file to be the model's
    defaults.
    """

    if not os.path.exists(self.midi_primer):
      tf.logging.warn('ERROR! No such primer file exists! %s', self.midi_primer)
      return

    self.primer_sequence = midi_io.midi_file_to_sequence_proto(self.midi_primer)
    quantized_seq = sequences_lib.QuantizedSequence()
    quantized_seq.from_note_sequence(self.primer_sequence,
                                     steps_per_quarter=4)
    extracted_melodies, _ = melodies_lib.extract_melodies(quantized_seq,
                                                          min_bars=0,
                                                          min_unique_pitches=1)
    self.primer = extracted_melodies[0]
    self.steps_per_bar = self.primer.steps_per_bar

  def prime_model(self, suppress_output=False):
    """Primes the model with its default midi primer."""
    with self.graph.as_default():
      if not suppress_output:
        tf.logging.info('Priming the model with MIDI file %s', self.midi_primer)

      # Convert primer Melody to model inputs.
      encoder = note_rnn_encoder_decoder.MelodyEncoderDecoder()
      seq = encoder.encode(self.primer)
      features = seq.feature_lists.feature_list['inputs'].feature
      primer_input = [list(i.float_list.value) for i in features]

      # Run model over primer sequence.
      primer_input_batch = np.tile([primer_input], (self.batch_size, 1, 1))
      self.state_value, softmax = self.session.run(
          [self.state_tensor, self.softmax],
          feed_dict={self.initial_state: self.state_value,
                     self.melody_sequence: primer_input_batch,
                     self.lengths: np.full(self.batch_size,
                                           len(self.primer),
                                           dtype=int)})
      priming_output = softmax[-1, :]
      self.priming_note = self.get_note_from_softmax(priming_output)

  def get_note_from_softmax(self, softmax):
    """Extracts a one-hot encoding of the most probable note.

    Args:
      softmax: Softmax probabilities over possible next notes.
    Returns:
      One-hot encoding of most probable note.
    """

    note_idx = np.argmax(softmax)
    note_enc = rl_tuner_ops.make_onehot([note_idx], rl_tuner_ops.NUM_CLASSES)
    return np.reshape(note_enc, (rl_tuner_ops.NUM_CLASSES))

  def __call__(self):
    """Allows the network to be called, as in the following code snippet!

        q_network = MelodyRNN(...)
        q_network()

    The q_network() operation can then be placed into a larger graph as a tf op.

    Note that to get actual values from call, must do session.run and feed in
    melody_sequence, lengths, and initial_state in the feed dict.

    Returns:
      Either softmax probabilities over notes, or raw logit scores.
    """
    with self.graph.as_default():
      with tf.variable_scope(self.scope, reuse=True):
        if self.softmax_within_graph:
          softmax, self.state_tensor = self.run_network_on_melody(
              self.melody_sequence, self.lengths, self.initial_state)
          return softmax
        else:
          logits, self.state_tensor = self.run_network_on_melody(
              self.melody_sequence, self.lengths, self.initial_state)
          return logits

  def run_training_batch(self):
    """Runs one batch of training data through the model.

    Uses a queue runner to pull one batch of data from the training files
    and run it through the model.

    Returns:
      A batch of softmax probabilities and model state vectors.
    """
    if self.training_file_list is None:
      tf.logging.warn('No training file path was provided, cannot run training'
                   'batch')
      return

    coord = tf.train.Coordinator()
    tf.train.start_queue_runners(sess=self.session, coord=coord)

    softmax, state, lengths = self.session.run([self.train_softmax,
                                                self.train_state,
                                                self.train_lengths])

    coord.request_stop()

    return softmax, state, lengths

  def get_next_note_from_note(self, note):
    """Given a note, uses the model to predict the most probable next note.

    Args:
      note: A one-hot encoding of the note.
    Returns:
      Next note in the same format.
    """
    with self.graph.as_default():
      with tf.variable_scope(self.scope, reuse=True):
        singleton_lengths = np.full(self.batch_size, 1, dtype=int)

        input_batch = np.reshape(note,
                                 (self.batch_size, 1, rl_tuner_ops.NUM_CLASSES))

        softmax, self.state_value = self.session.run(
            [self.softmax, self.state_tensor],
            {self.melody_sequence: input_batch,
             self.initial_state: self.state_value,
             self.lengths: singleton_lengths})

        return self.get_note_from_softmax(softmax)

  def variables(self):
    """Gets names of all the variables in the graph belonging to this model.

    Returns:
      List of variable names.
    """
    with self.graph.as_default():
      return [v for v in tf.all_variables() if v.name.startswith(self.scope)]
