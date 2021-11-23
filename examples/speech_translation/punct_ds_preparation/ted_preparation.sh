python prepare_big_data_for_punctuation_capitalization_task_simple.py \
  --output_dir /media/apeganov/DATA/prepared_TED_48_65_23.11.2021 \
  --corpus_types TED \
  --create_model_input \
  --bert_labels \
  --autoregressive_labels \
  --sequence_length_range 48 65 \
  --allowed_punctuation '.,?' \
  --only_first_punctuation_character_after_word_in_autoregressive \
  --no_label_if_all_characters_are_upper_case \
  --input_files ~/data/TED_Talks/en-ja/train.tags.en-ja.en
  --num_jobs 24