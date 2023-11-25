import tensorflow as tf
import os
import joblib
import re
import os
import numpy as np
import nltk
from nltk.translate.bleu_score import sentence_bleu


def convert_to_sets(file_path, length=2):
    sets_of_lines = []
    current_set = []

    with open(file_path, 'r', encoding='utf-8') as f:
        all_lines = [line.rstrip('\n') for line in f]

    for line in all_lines:
        # Lowercase and clean the line
        line = line.lower().replace(',', '').replace('.', '')
        line = re.sub(r'\$\d+(\.\d{2})?', '<price>', line)

        if line.startswith('customer:') and not current_set:
            # Start a new set if the current set is empty and line starts with 'Customer:'
            current_set.append(line)
        elif line.startswith('system:') and current_set:
            # Add line to set and check if it's time to store the set
            current_set.append(line)
            if len(current_set) >= length:
                sets_of_lines.append(current_set)
                current_set = []
        elif current_set:
            # Add line to current set if it's already started
            current_set.append(line)

        # Check if the current set reached the maximum length
        if len(current_set) == length + 1:
            # Remove the last line and save the set
            last_line = current_set.pop()
            sets_of_lines.append(current_set)
            current_set = [last_line]  # Start a new set with the last line

    # Add any leftover lines as a final set
    if current_set:
        sets_of_lines.append(current_set)

    return sets_of_lines


def file_to_sequences(file_path, intent, length=2):
    sets_of_lines = convert_to_sets(file_path, length)
    processed_sequences = []
    intents = []
    for set_ in sets_of_lines:
        # Concatenate the lines in the set into a single string
        concatenated_sequence = " ".join(set_)
        # Add the GPT-2 end-of-text token
        sequence_with_token = f"{concatenated_sequence}"
        processed_sequences.append(sequence_with_token)
        intents.append(intent)
    return processed_sequences, intents


def preprocess_for_generation(final_sequences, tokenizer=None, train=True):
    # Tokenize the input sequences
    if tokenizer:
        tokenizer.pad_token = tokenizer.eos_token
        tokenized_data = tokenizer(final_sequences, max_length=150, truncation=True, padding=True, return_tensors="tf")
    else:
        tokenized_data = final_sequences

    # If in training mode, prepare data for training (input and target)
    if train:
        # Extract input IDs and attention masks
        input_ids = tokenized_data['input_ids']
        attention_mask = tokenized_data['attention_mask']

        # Slice input and attention mask for X and Y
        input_ids_X = input_ids[:, :-1]
        input_ids_Y = input_ids[:, 1:]
        attention_mask_X = attention_mask[:, :-1]

        dataset = tf.data.Dataset.from_tensor_slices(
            ({
                 'input_ids': input_ids_X,
                 'attention_mask': attention_mask_X
             },
             {
                 'input_ids': input_ids_Y
             }
            ))
    else:
        # For inference, provide input IDs and attention masks
        dataset = tf.data.Dataset.from_tensor_slices(tokenized_data)

    # Prefetch data for efficient loading
    return dataset.shuffle(1000).batch(16).prefetch(1)


def ordinal_encode(intents, intent_to_label=None):
    """
    Preprocess a list of intent strings into integer labels using tf.lookup.

    Args:
    - intents (list of str): List of intent strings.
    - intent_to_label (tf.lookup.StaticVocabularyTable, optional): Vocabulary table for mapping intents to labels.
      If not provided, a default table will be created.

    Returns:
    - intent_labels (tf.Tensor): Tensor of integer labels corresponding to the input intents.
    - intent_to_label (tf.lookup.StaticVocabularyTable): Vocabulary table used for mapping intents to labels.
    """

    if intent_to_label is None:
        # Create a default vocabulary table if not provided
        lookup_init = tf.lookup.KeyValueTensorInitializer(
            keys=[b'order', b'complain', b'enquiry'],
            values=[0, 1, 2],
            key_dtype=tf.string,
            value_dtype=tf.int64,
        )
        intent_to_label = tf.lookup.StaticVocabularyTable(
            lookup_init,
            num_oov_buckets=1,
        )

    # Convert intent strings to integer labels
    intent_labels = intent_to_label.lookup(tf.constant(intents, dtype=tf.string))

    return intent_labels, intent_to_label


def preprocess_for_intent(final_sequences, intents=None, tokenizer=None, train=True):
    if tokenizer:
        tokenized_data = tokenizer(final_sequences, max_length=40, truncation=True, padding='max_length', return_tensors="tf")
    else:
        tokenized_data = final_sequences

    input_ids = tokenized_data['input_ids']
    attention_mask = tokenized_data['attention_mask']

    dataset = tf.data.Dataset.from_tensor_slices({
        "input_ids": input_ids,
        "attention_mask": attention_mask
    })

    # at prediction time
    if not train:
        return dataset.shuffle(10000).batch(16).prefetch(1)

    # at training time
    intents, _ = ordinal_encode(intents)
    intents = tf.data.Dataset.from_tensor_slices(intents).map(lambda intent: tf.one_hot(intent, 3))
    dataset = tf.data.Dataset.zip((dataset, intents))
    return dataset.shuffle(10000).batch(16).prefetch(1)


def save_file(filepath, value):
    with open(filepath, 'wb') as f:
        joblib.dump(value, f)


class OneCycleLRSchedule(tf.keras.callbacks.Callback):
    def __init__(self, max_lr, total_steps, lr_start=1e-5, lr_end=1e-6, div_factor=25, pct_start=0.3):
        super(OneCycleLRSchedule, self).__init__()

        self.max_lr = max_lr  # Maximum learning rate (peak)
        self.lr_start = lr_start  # Initial learning rate
        self.lr_end = lr_end  # Final learning rate
        self.div_factor = div_factor  # Factor to divide max_lr for the minimum
        self.pct_start = pct_start  # Phase 1 percentage
        self.total_steps = total_steps  # Total steps in the cycle

        self.phase_1_steps = np.floor(pct_start * total_steps)  # Steps in the first phase
        self.phase_2_steps = total_steps - self.phase_1_steps  # Steps in the second phase

    def on_train_begin(self, logs=None):
        self.set_lr(self.lr_start)

    def on_train_batch_begin(self, batch, logs=None):
        if batch < self.phase_1_steps:
            # Phase 1: Linearly increase the learning rate
            lr = (self.max_lr - self.lr_start) / self.phase_1_steps * batch + self.lr_start
        else:
            # Phase 2: Cosine annealing to the final learning rate
            progress = (batch - self.phase_1_steps) / self.phase_2_steps
            lr = self.lr_end + 0.5 * (self.max_lr - self.lr_end) * (1 + np.cos(np.pi * progress))
        self.set_lr(lr)

    def on_epoch_end(self, epoch, logs=None):
        print(self.model.optimizer.lr)

    def set_lr(self, lr):
        tf.keras.backend.set_value(self.model.optimizer.lr, lr)

    def get_lr(self):
        return tf.keras.backend.get_value(self.model.optimizer.lr)