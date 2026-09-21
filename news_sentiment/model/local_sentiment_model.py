"""
local_sentiment_model.py

Loads your fine-tuned sentiment classifier (from train_sentiment_model.py)
and provides the same interface the scraper expects, so it's a drop-in
swap for the Anthropic-API-based classifier — no internet call, no
per-request cost, runs entirely on your machine.

Usage:
    from local_sentiment_model import LocalSentimentClassifier

    classifier = LocalSentimentClassifier("./sentiment_model")
    result = classifier.classify("Nvidia beats earnings estimates, raises guidance")
    # -> {"sentiment": "Positive", "confidence": 0.94}

    results = classifier.classify_batch(["headline 1", "headline 2", ...])
"""

from transformers import AutoTokenizer, AutoModelForSequenceClassification
import torch


class LocalSentimentClassifier:
    def __init__(self, model_dir="./sentiment_model"):
        self.tokenizer = AutoTokenizer.from_pretrained(model_dir)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_dir)
        self.model.eval()
        self.id2label = self.model.config.id2label  # e.g. {0: "negative", 1: "neutral", 2: "positive"}

    def classify(self, text):
        inputs = self.tokenizer(text, truncation=True, padding=True, max_length=128, return_tensors="pt")
        with torch.no_grad():
            logits = self.model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)[0]
        pred_id = int(torch.argmax(probs))
        label = self.id2label[pred_id].capitalize()
        confidence = float(probs[pred_id])
        return {"sentiment": label, "confidence": round(confidence, 3)}

    def classify_batch(self, texts, batch_size=32):
        results = []
        for i in range(0, len(texts), batch_size):
            chunk = texts[i:i + batch_size]
            inputs = self.tokenizer(chunk, truncation=True, padding=True, max_length=128, return_tensors="pt")
            with torch.no_grad():
                logits = self.model(**inputs).logits
            probs = torch.softmax(logits, dim=-1)
            for row in probs:
                pred_id = int(torch.argmax(row))
                results.append({
                    "sentiment": self.id2label[pred_id].capitalize(),
                    "confidence": round(float(row[pred_id]), 3),
                })
        return results


if __name__ == "__main__":
    # Quick manual test once you've trained a model
    classifier = LocalSentimentClassifier("./sentiment_model")
    samples = [
        "Nvidia beats earnings estimates and raises full-year guidance",
        "Exxon shares tumble as refining margins compress",
        "The company filed its routine quarterly report on schedule",
    ]
    for s in samples:
        print(classifier.classify(s), "—", s)
