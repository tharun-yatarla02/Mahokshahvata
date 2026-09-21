"""
train_sentiment_model.py

Fine-tunes a small transformer (DistilBERT) into a 3-class financial news
sentiment classifier (Positive / Negative / Neutral), trained on the
Financial PhraseBank dataset — ~5,000 sentences from financial news,
hand-labeled by finance professionals. This is the standard public
benchmark dataset for this exact task.

This is YOUR model once trained: you're fine-tuning a general-purpose
base model into a task-specific classifier, which is standard practice
and a legitimate "I trained this" deliverable for a project write-up.

Setup:
    pip install transformers datasets torch scikit-learn accelerate

Run:
    python train_sentiment_model.py

Output:
    Saves the fine-tuned model + tokenizer to ./sentiment_model/
    (load it later with local_sentiment_model.py)

Hardware note:
    Runs on CPU but slowly (~20-40 min for 3 epochs on ~5k sentences).
    Free GPU (e.g. Google Colab) finishes this in a couple of minutes.
    Reduce NUM_EPOCHS or use a subset of the data if you just want to
    confirm the pipeline works end-to-end before a longer real run.

To train on your OWN labeled headlines instead/in addition:
    Prepare a CSV with columns "text,label" (label as one of
    "positive","negative","neutral"), then call
    load_custom_csv("your_file.csv") and concatenate it with the
    Financial PhraseBank dataset before training (see the __main__
    block for where to plug this in).
"""

import numpy as np
from datasets import load_dataset, concatenate_datasets, Dataset
from transformers import (
    AutoTokenizer,
    AutoModelForSequenceClassification,
    TrainingArguments,
    Trainer,
)
from sklearn.metrics import accuracy_score, f1_score
import pandas as pd

BASE_MODEL = "distilbert-base-uncased"
OUTPUT_DIR = "./sentiment_model"
NUM_EPOCHS = 3
BATCH_SIZE = 16
LEARNING_RATE = 2e-5

LABEL_NAMES = ["negative", "neutral", "positive"]  # index order matters
LABEL2ID = {name: i for i, name in enumerate(LABEL_NAMES)}
ID2LABEL = {i: name for i, name in enumerate(LABEL_NAMES)}


def load_financial_phrasebank():
    """Loads the 'all agree' subset — sentences where all annotators agreed
    on the label, which is the cleanest, least noisy slice to train on."""
    ds = load_dataset("financial_phrasebank", "sentences_allagree")
    # The dataset only has a 'train' split; we carve out our own val/test.
    ds = ds["train"].train_test_split(test_size=0.2, seed=42)
    return ds["train"], ds["test"]


def load_custom_csv(path):
    """Load your own labeled headlines: CSV with columns text,label
    (label one of: positive, negative, neutral)."""
    df = pd.read_csv(path)
    df["label"] = df["label"].str.lower().map(LABEL2ID)
    if df["label"].isna().any():
        raise ValueError("Found labels outside positive/negative/neutral — check your CSV.")
    return Dataset.from_pandas(df[["text", "label"]], preserve_index=False)


def compute_metrics(eval_pred):
    logits, labels = eval_pred
    predictions = np.argmax(logits, axis=-1)
    return {
        "accuracy": accuracy_score(labels, predictions),
        "f1_macro": f1_score(labels, predictions, average="macro"),
    }


def main():
    print(f"Loading base model: {BASE_MODEL}")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    model = AutoModelForSequenceClassification.from_pretrained(
        BASE_MODEL,
        num_labels=len(LABEL_NAMES),
        id2label=ID2LABEL,
        label2id=LABEL2ID,
    )

    print("Loading Financial PhraseBank dataset...")
    train_ds, eval_ds = load_financial_phrasebank()

    # --- To add your own labeled data, uncomment and adjust: ---
    # custom_ds = load_custom_csv("my_labeled_headlines.csv")
    # train_ds = concatenate_datasets([train_ds, custom_ds])

    def tokenize(batch):
        return tokenizer(batch["sentence"], truncation=True, padding="max_length", max_length=128)

    train_ds = train_ds.map(tokenize, batched=True)
    eval_ds = eval_ds.map(tokenize, batched=True)

    training_args = TrainingArguments(
        output_dir="./training_checkpoints",
        num_train_epochs=NUM_EPOCHS,
        per_device_train_batch_size=BATCH_SIZE,
        per_device_eval_batch_size=BATCH_SIZE,
        learning_rate=LEARNING_RATE,
        eval_strategy="epoch",
        save_strategy="epoch",
        load_best_model_at_end=True,
        metric_for_best_model="f1_macro",
        logging_steps=25,
        report_to="none",
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        compute_metrics=compute_metrics,
    )

    print("Training...")
    trainer.train()

    print("\nFinal evaluation:")
    metrics = trainer.evaluate()
    print(metrics)

    print(f"\nSaving model to {OUTPUT_DIR}")
    trainer.save_model(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)
    print("Done. Load it for inference with local_sentiment_model.py")


if __name__ == "__main__":
    main()
