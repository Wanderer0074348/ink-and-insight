import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
import io
import numpy as np
from pdf2image import convert_from_path
import os
import base64
import hashlib
import json
from typing import List, Dict, Tuple

CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "cached_data"
)
os.makedirs(CACHE_DIR, exist_ok=True)


def get_cache_key(file_path: str) -> str:
    with open(file_path, "rb") as file:
        return hashlib.md5(file.read()).hexdigest()


def load_from_cache(cache_key: str):
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    if os.path.exists(cache_file):
        with open(cache_file, "r") as file:
            return json.load(file)
    return None


def save_to_cache(cache_key: str, data) -> None:
    cache_file = os.path.join(CACHE_DIR, f"{cache_key}.json")
    with open(cache_file, "w") as file:
        json.dump(data, file)


def process_image(args: Tuple) -> List[Dict]:
    image, api_key, page_num = args
    try:
        img_byte_arr = io.BytesIO()
        image.save(img_byte_arr, format="PNG")
        img_base64 = base64.b64encode(img_byte_arr.getvalue()).decode()

        url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
        payload = {
            "requests": [
                {
                    "image": {"content": img_base64},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION", "maxResults": 50}],
                }
            ]
        }

        response = requests.post(url, json=payload, timeout=30)
        if response.status_code != 200:
            print(f"Error {response.status_code}: {response.text}")
            return []

        result = response.json()
        page_features = []

        if "responses" in result and result["responses"]:
            response_data = result["responses"][0]
            if "fullTextAnnotation" in response_data:
                text_data = response_data["fullTextAnnotation"]

                for page in text_data.get("pages", []):
                    for block in page.get("blocks", []):
                        for paragraph in block.get("paragraphs", []):
                            words = paragraph.get("words", [])
                            if words:
                                page_features.append(
                                    {
                                        "confidence": paragraph.get("confidence", 0),
                                        "word_count": len(words),
                                        "symbol_density": sum(
                                            1
                                            for word in words
                                            for symbol in word.get("symbols", [])
                                            if not symbol.get("text", "").isalnum()
                                        )
                                        / len(words)
                                        if words
                                        else 0,
                                        "line_breaks": sum(
                                            1
                                            for word in words
                                            for symbol in word.get("symbols", [])
                                            if symbol.get("property", {})
                                            .get("detectedBreak", {})
                                            .get("type")
                                        ),
                                        "average_symbol_confidence": sum(
                                            symbol.get("confidence", 0)
                                            for word in words
                                            for symbol in word.get("symbols", [])
                                        )
                                        / sum(
                                            1
                                            for word in words
                                            for _ in word.get("symbols", [])
                                        ),
                                    }
                                )

        return page_features

    except Exception as e:
        print(f"Error processing page {page_num}: {str(e)}")
        return []


def extract_handwriting_features(images: List, api_key: str) -> List:
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(process_image, (image, api_key, i))
            for i, image in enumerate(images)
        ]
        return [future.result() for future in as_completed(futures)]


def compute_handwriting_similarity(pdf_path1: str, pdf_path2: str) -> Tuple:
    try:
        images1 = convert_from_path(pdf_path1)
        images2 = convert_from_path(pdf_path2)
        api_key = os.environ.get("GOOGLE_CLOUD_API_KEY")

        cache_key = hashlib.md5(
            (get_cache_key(pdf_path1) + get_cache_key(pdf_path2)).encode()
        ).hexdigest()
        if cached := load_from_cache(cache_key):
            return cached

        features1 = extract_handwriting_features(images1, api_key)
        features2 = extract_handwriting_features(images2, api_key)

        anomalies1, variations1 = detect_internal_anomalies(features1)
        anomalies2, variations2 = detect_internal_anomalies(features2)

        similarity, feature_scores = compare_handwriting_features(features1, features2)
        response = (
            float(np.clip(similarity, 0, 1)),
            feature_scores,
            anomalies1,
            anomalies2,
            variations1,
            variations2,
        )

        save_to_cache(cache_key, response)
        return response

    except Exception as e:
        print(f"Handwriting similarity error: {str(e)}")
        raise Exception(f"Error computing handwriting similarity: {str(e)}")


def compare_handwriting_features(
    features1: List, features2: List
) -> Tuple[float, Dict]:
    if not features1 or not features2 or not features1[0] or not features2[0]:
        return 0.0, {}

    flat_features1 = [f for page_features in features1 for f in page_features]
    flat_features2 = [f for page_features in features2 for f in page_features]

    if not flat_features1 or not flat_features2:
        return 0.0, {}

    conf_sim = 1 - abs(
        np.mean([f["confidence"] for f in flat_features1])
        - np.mean([f["confidence"] for f in flat_features2])
    )

    symbol_density_sim = 1 - abs(
        np.mean([f["symbol_density"] for f in flat_features1])
        - np.mean([f["symbol_density"] for f in flat_features2])
    )

    line_break_sim = 1 - abs(
        np.mean([f["line_breaks"] for f in flat_features1])
        - np.mean([f["line_breaks"] for f in flat_features2])
    )

    avg_conf_sim = 1 - abs(
        np.mean([f["average_symbol_confidence"] for f in flat_features1])
        - np.mean([f["average_symbol_confidence"] for f in flat_features2])
    )

    feature_scores = {
        "confidence_similarity": float(np.clip(conf_sim, 0, 1)),
        "symbol_density_similarity": float(np.clip(symbol_density_sim, 0, 1)),
        "line_break_similarity": float(np.clip(line_break_sim, 0, 1)),
        "average_confidence_similarity": float(np.clip(avg_conf_sim, 0, 1)),
    }

    weights = {
        "confidence": 0.3,
        "symbol_density": 0.3,
        "line_breaks": 0.2,
        "avg_confidence": 0.2,
    }

    similarity = (
        weights["confidence"] * conf_sim
        + weights["symbol_density"] * symbol_density_sim
        + weights["line_breaks"] * line_break_sim
        + weights["avg_confidence"] * avg_conf_sim
    )

    return float(np.clip(similarity, 0, 1)), feature_scores


def detect_internal_anomalies(features: List) -> Tuple[List, List]:
    anomalies = []
    page_variations = []

    if not features or not isinstance(features[0], list):
        return [], []

    for page_num, page_features in enumerate(features):
        if not page_features:
            continue

        page_confidence_mean = np.mean([f["confidence"] for f in page_features])
        page_symbol_density_mean = np.mean([f["symbol_density"] for f in page_features])
        page_line_breaks_mean = np.mean([f["line_breaks"] for f in page_features])

        page_characteristics = {
            "page_number": page_num + 1,
            "confidence": page_confidence_mean,
            "symbol_density": page_symbol_density_mean,
            "line_breaks": page_line_breaks_mean,
        }

        page_anomalies = detect_page_anomalies(page_features, page_num)
        anomalies.extend(page_anomalies)
        page_variations.append(page_characteristics)

    if len(page_variations) > 1:
        variations = analyze_page_variations(page_variations)
        return anomalies, variations

    return anomalies, []


def detect_page_anomalies(features: List, page_num: int) -> List[Dict]:
    anomalies = []
    threshold = 2.0

    confidence_mean = np.mean([f["confidence"] for f in features])
    confidence_std = np.std([f["confidence"] for f in features])

    symbol_density_mean = np.mean([f["symbol_density"] for f in features])
    symbol_density_std = np.std([f["symbol_density"] for f in features])

    line_breaks_mean = np.mean([f["line_breaks"] for f in features])
    line_breaks_std = np.std([f["line_breaks"] for f in features])

    for i, feature in enumerate(features):
        anomaly = {}

        if abs(feature["confidence"] - confidence_mean) > threshold * confidence_std:
            anomaly["confidence"] = {
                "value": feature["confidence"],
                "mean": confidence_mean,
                "deviation": abs(feature["confidence"] - confidence_mean)
                / confidence_std,
            }

        if (
            abs(feature["symbol_density"] - symbol_density_mean)
            > threshold * symbol_density_std
        ):
            anomaly["symbol_density"] = {
                "value": feature["symbol_density"],
                "mean": symbol_density_mean,
                "deviation": abs(feature["symbol_density"] - symbol_density_mean)
                / symbol_density_std,
            }

        if abs(feature["line_breaks"] - line_breaks_mean) > threshold * line_breaks_std:
            anomaly["line_breaks"] = {
                "value": feature["line_breaks"],
                "mean": line_breaks_mean,
                "deviation": abs(feature["line_breaks"] - line_breaks_mean)
                / line_breaks_std,
            }

        if anomaly:
            anomaly["paragraph_index"] = i
            anomaly["page_number"] = page_num + 1
            anomalies.append(anomaly)

    return anomalies


def analyze_page_variations(page_characteristics: List[Dict]) -> List[Dict]:
    variations = []
    threshold = 0.15

    for i in range(1, len(page_characteristics)):
        prev_page = page_characteristics[i - 1]
        curr_page = page_characteristics[i]

        variation = {
            "from_page": prev_page["page_number"],
            "to_page": curr_page["page_number"],
            "changes": [],
        }

        changes = {
            "confidence": abs(curr_page["confidence"] - prev_page["confidence"]),
            "symbol_density": abs(
                curr_page["symbol_density"] - prev_page["symbol_density"]
            ),
            "line_breaks": abs(curr_page["line_breaks"] - prev_page["line_breaks"]),
        }

        for change_type, value in changes.items():
            if value > threshold:
                variation["changes"].append(
                    {
                        "type": change_type,
                        "difference": value,
                        "description": f"{change_type.replace('_', ' ').title()} changed by {(value * 100):.1f}%",
                    }
                )

        if variation["changes"]:
            variations.append(variation)

    return variations
