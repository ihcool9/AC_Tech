# Vercel 진입점(프로젝트 Root Directory 가 저장소 최상위일 때). 실제 앱은 avionics_weekly/app.py.
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "avionics_weekly"))

from app import app  # noqa: E402,F401
