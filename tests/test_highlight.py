import requests


url = "http://localhost:8180/v1/highlight"
payload = {
    "model": "semantic-highlight-bilingual-v1",
    "question": "脱水的症状有哪些？",
    "context": "脱水是指身体失去的水分多于摄入的水分。常见症状包括口渴和口干。",
    "threshold": 0.5,
    "language": "zh",
    "return_sentence_metrics": True
}

response = requests.post(url, json=payload)
print(response.json())