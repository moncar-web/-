"""이슈톡톡 이미지 일괄 생성 스크립트 (Google Gemini API / Imagen)

4단계 프롬프트 파일(.md)에서 이미지 프롬프트를 읽어
Imagen으로 한 장씩 생성하고 0001.png, 0002.png ... 로 저장합니다.

사용 전 준비:
    pip install google-genai
    (Mac/Linux) export GEMINI_API_KEY="발급받은_키"
    (Windows)   setx GEMINI_API_KEY "발급받은_키"   → 새 터미널에서 실행

사용 예:
    # 1~5번만 시험 생성
    python generate_images.py --end 5
    # 전체 생성 (이미 만든 파일은 건너뜀, 중단 후 다시 실행하면 이어서 생성)
    python generate_images.py
"""
import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MD = HERE.parent / "4단계_이미지_영상_프롬프트_v2.md"


def load_prompts(md_path):
    text = Path(md_path).read_text(encoding="utf-8")
    image_part = text.split("## 🎬")[0]  # 영상 프롬프트 구역은 제외
    rows = re.findall(r"^\*\*(\d+)\.\*\* \[(.*?)\] (.*)$", image_part, re.M)
    return [(int(n), label, prompt.strip()) for n, label, prompt in rows]


def main():
    ap = argparse.ArgumentParser(description="Imagen 이미지 일괄 생성")
    ap.add_argument("--md", default=str(DEFAULT_MD), help="프롬프트 .md 파일 경로")
    ap.add_argument("--out", default=str(HERE.parent / "images"), help="저장 폴더")
    ap.add_argument("--model", default="imagen-4.0-fast-generate-001",
                    help="imagen-4.0-fast-generate-001 / imagen-4.0-generate-001 / imagen-4.0-ultra-generate-001")
    ap.add_argument("--start", type=int, default=1, help="시작 번호")
    ap.add_argument("--end", type=int, default=None, help="끝 번호 (포함)")
    ap.add_argument("--aspect", default="16:9", help="화면 비율")
    ap.add_argument("--delay", type=float, default=2.0, help="요청 사이 대기(초)")
    ap.add_argument("--retries", type=int, default=4, help="실패 시 재시도 횟수")
    ap.add_argument("--list", action="store_true", help="생성하지 않고 프롬프트 목록 CSV만 저장")
    args = ap.parse_args()

    prompts = load_prompts(args.md)
    if not prompts:
        sys.exit(f"프롬프트를 찾지 못했습니다: {args.md}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.list:
        csv_path = out / "prompts.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["번호", "파일명", "대본", "프롬프트"])
            for n, label, prompt in prompts:
                w.writerow([n, f"{n:04d}.png", label, prompt])
        print(f"{len(prompts)}개 프롬프트를 {csv_path} 에 저장했습니다.")
        return

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY 환경 변수가 없습니다. 스크립트 맨 위 설명을 참고해 주세요.")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    config = types.GenerateImagesConfig(
        number_of_images=1,
        aspect_ratio=args.aspect,
        person_generation="allow_all",  # 아이 등장 장면(꽃님이)이 있어 필요
    )

    targets = [p for p in prompts
               if p[0] >= args.start and (args.end is None or p[0] <= args.end)]
    failed = []
    for i, (n, label, prompt) in enumerate(targets, 1):
        path = out / f"{n:04d}.png"
        if path.exists():
            continue
        for attempt in range(1, args.retries + 1):
            try:
                resp = client.models.generate_images(model=args.model, prompt=prompt, config=config)
                if not resp.generated_images:
                    raise RuntimeError("이미지가 비어 있음 (안전 필터에 걸렸을 수 있음)")
                path.write_bytes(resp.generated_images[0].image.image_bytes)
                print(f"[{i}/{len(targets)}] {path.name} 완료  {label[:30]}")
                break
            except Exception as e:
                wait = args.delay * (2 ** attempt)
                print(f"[{i}/{len(targets)}] {n:04d} 실패 {attempt}회: {e}  → {wait:.0f}초 뒤 재시도")
                time.sleep(wait)
        else:
            failed.append((n, label, prompt))
        time.sleep(args.delay)

    if failed:
        fail_path = out / "failed.csv"
        with open(fail_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["번호", "대본", "프롬프트"])
            w.writerows(failed)
        print(f"\n실패 {len(failed)}개 → {fail_path} (다시 실행하면 실패한 것만 재시도합니다)")
    else:
        print("\n모두 완료했습니다.")


if __name__ == "__main__":
    main()
