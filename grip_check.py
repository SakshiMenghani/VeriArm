"""Grip confirmation with the wrist camera (the vacuum gripper's is_holding_something is not reliable).

The camera is fixed to the arm next to the suction cup, looking down.
  1. photo at the box top, suction on   (frame A: mostly box top, close up)
  2. lift straight up                    (frame B)
If the box is held, it rises with the camera: B looks like A. If not, the camera moved away from the
box: it looks smaller, more table/blue sheet shows, and B differs a lot from A.

  python grip_check.py A.jpg B.jpg      score two saved photos (no robot)
"""
import sys
from pathlib import Path

import cv2
import numpy as np
from viam.components.camera import Camera
from viam.robot.client import RobotClient

from robot import CAMERA

CAPTURES = Path(__file__).parent / "captures"
HELD_MIN_SIMILARITY = 0.6  # fallback only (correlation is unreliable on plain cardboard)
HELD_MIN_SCALE = 0.85      # held: things stay the same size (~1.0); not held: the camera moved ~8 cm away (~0.6)
MIN_MATCHES = 12
SIZE = (160, 90)           # compare small, blurred grayscale images: robust to noise and small vibrations


async def wrist_photo(machine: RobotClient) -> np.ndarray:
    images, _ = await Camera.from_robot(machine, CAMERA).get_images()
    color = next(i for i in images if i.mime_type in ("image/jpeg", "image/png"))
    return cv2.imdecode(np.frombuffer(color.data, np.uint8), cv2.IMREAD_COLOR)


def _prep(bgr: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(cv2.resize(bgr, SIZE, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY).astype(np.float32)
    # ignore the bottom strip: the suction cups themselves are always in view there
    g = g[: int(SIZE[1] * 0.8)]
    return cv2.GaussianBlur(g, (5, 5), 0)


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Pearson correlation of the two prepared images, -1..1 (1 = identical up to brightness)."""
    x, y = _prep(a).ravel(), _prep(b).ravel()
    x, y = x - x.mean(), y - y.mean()
    d = float(np.sqrt((x @ x) * (y @ y)))
    return float(x @ y / d) if d > 1e-6 else 0.0


def scale_change(a: np.ndarray, b: np.ndarray) -> float | None:
    """How much bigger/smaller things look in b than in a (1.0 = same size), from matched features.
    None if there aren't enough matches to tell."""
    ga = cv2.cvtColor(cv2.resize(a, (640, 360)), cv2.COLOR_BGR2GRAY)[:290]  # skip the suction cups at the bottom
    gb = cv2.cvtColor(cv2.resize(b, (640, 360)), cv2.COLOR_BGR2GRAY)[:290]
    orb = cv2.ORB_create(1500)
    ka, da = orb.detectAndCompute(ga, None)
    kb, db = orb.detectAndCompute(gb, None)
    if da is None or db is None:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < MIN_MATCHES:
        return None
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    M, inliers = cv2.estimateAffinePartial2D(pa, pb, method=cv2.RANSAC, ransacReprojThreshold=4.0)
    if M is None or inliers is None or int(inliers.sum()) < MIN_MATCHES:
        return None
    return float(np.hypot(M[0, 0], M[1, 0]))


def judge(before: np.ndarray, after: np.ndarray, tag: str = "grip") -> tuple[bool, float]:
    """(held?, score). Score = size change between the photos (1.0 = held); falls back to image similarity
    if there's too little texture to match. Saves both photos side by side to captures/<tag>.jpg."""
    scale = scale_change(before, after)
    if scale is not None:
        score, held, how = scale, scale >= HELD_MIN_SCALE, "size"
    else:
        score = similarity(before, after)
        held, how = score >= HELD_MIN_SIMILARITY, "similarity"
    CAPTURES.mkdir(exist_ok=True)
    side = np.hstack([cv2.resize(before, (640, 360)), cv2.resize(after, (640, 360))])
    cv2.putText(side, f"at box top | after lift: {how} {score:.2f} -> {'HELD' if held else 'NOT held'}",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    cv2.imwrite(str(CAPTURES / f"{tag}.jpg"), side)
    return held, score


if __name__ == "__main__":
    a, b = (cv2.imread(p) for p in sys.argv[1:3])
    print("similarity %.3f -> %s" % (similarity(a, b), "held" if similarity(a, b) >= HELD_MIN_SIMILARITY else "NOT held"))
