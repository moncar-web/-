"""이슈톡톡 자동 편집 스크립트: 이미지 + 음성 + 자막 → mp4

구간표_압축판.csv 순서대로 이미지를 놓고, 이미지마다 천천히 확대·이동하는
효과(켄 번스)를 준 뒤 내레이션과 하단 자막을 입혀 mp4 한 편으로 만듭니다.

준비:
    pip install imageio-ffmpeg edge-tts
    (ffmpeg가 이미 설치되어 있으면 그것을 우선 사용합니다)

음성 방식 (셋 중 하나):
    --tts edge          이미지마다 대본을 무료 AI 음성(Microsoft Edge)으로 읽어 정확히 맞춤 (추천)
    --audio 파일.mp3     직접 만든 내레이션 한 파일을 사용 (예상 시간을 전체 길이에 맞춰 늘리거나 줄임)
    (지정 안 함)         음성 없이 예상 시간으로만 만듦 (미리보기용)

사용 예:
    python make_video.py --tts edge --channel "이슈톡톡" --end 10     # 앞 10장만 시험
    python make_video.py --tts edge --channel "이슈톡톡"               # 전체
"""
import argparse
import asyncio
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

MOTIONS = [
    # (zoom, x, y) : zoompan 식. N = 전체 프레임 수
    ("1+0.12*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),        # 천천히 확대
    ("1.12-0.12*on/{N}", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),     # 천천히 축소
    ("1.1", "(iw-iw/zoom)*on/{N}", "ih/2-(ih/zoom/2)"),               # 왼쪽 → 오른쪽
    ("1.1", "(iw-iw/zoom)*(1-on/{N})", "ih/2-(ih/zoom/2)"),           # 오른쪽 → 왼쪽
]


def ffmpeg_exe():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg가 없습니다. 'pip install imageio-ffmpeg'를 실행해 주세요.")


FF = ffmpeg_exe()


def run(args):
    r = subprocess.run([FF, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"ffmpeg 오류:\n{r.stderr[-2000:]}")


def media_duration(path):
    r = subprocess.run([FF, "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", r.stderr)
    if not m:
        sys.exit(f"길이를 읽지 못했습니다: {path}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def load_rows(csv_path):
    with open(csv_path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [dict(n=int(r["이미지번호"]), file=r["파일명"], sec=float(r["길이(초)"]), text=r["대본"]) for r in rows]


def split_sentences(text, max_len=34):
    parts = re.split(r'(?<=[.?!…"”])\s+', text.strip())
    out = []
    for p in parts:
        while len(p) > max_len:
            cut = p.rfind(",", 0, max_len)
            cut = cut + 1 if cut > 8 else p.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            out.append(p)
    return out


def parse_srt(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        st = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        en = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        body = " ".join(block[m.end():].strip().splitlines())
        cues.append((st, en, body))
    if not cues:
        sys.exit(f"자막 파일에서 시간을 읽지 못했습니다: {path}")
    return cues


def align_to_srt(rows, cues):
    """대본 글자 수 누적 비율을 자막 글자 수 누적 비율에 맞춰 이미지 경계 시각을 구함.
    Vrew에서 문장을 조금 고쳤어도 전체 흐름으로 맞춰지도록 비율로 계산합니다."""
    def n(s):
        return len(re.sub(r"\s", "", s))
    cue_chars = [max(n(c[2]), 1) for c in cues]
    total_cue = sum(cue_chars)
    total_script = sum(n(r["text"]) for r in rows) or 1
    bounds, acc = [cues[0][0]], 0
    for r in rows:
        acc += n(r["text"])
        target = acc / total_script * total_cue
        run_c = 0
        for (st, en, _), c in zip(cues, cue_chars):
            if run_c + c >= target:
                bounds.append(st + (en - st) * (target - run_c) / c)
                break
            run_c += c
        else:
            bounds.append(cues[-1][1])
    return bounds


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


async def tts_all(rows, tts_dir, voice, rate):
    import edge_tts
    tts_dir.mkdir(parents=True, exist_ok=True)
    for r in rows:
        path = tts_dir / f"{r['n']:04d}.mp3"
        if path.exists() and path.stat().st_size > 0:
            continue
        for attempt in range(4):
            try:
                await edge_tts.Communicate(r["speak"], voice, rate=rate).save(str(path))
                print(f"  음성 {path.name} 완료")
                break
            except Exception as e:
                print(f"  음성 {path.name} 실패({e}), 재시도")
                await asyncio.sleep(3 * (attempt + 1))
        else:
            sys.exit(f"음성 생성 실패: {path.name}")


def main():
    ap = argparse.ArgumentParser(description="이미지+음성+자막 → mp4 자동 편집")
    ap.add_argument("--csv", default=str(ROOT / "구간표_압축판.csv"))
    ap.add_argument("--images", default=str(ROOT / "images"))
    ap.add_argument("--out", default=str(ROOT / "output" / "영상.mp4"))
    ap.add_argument("--work", default=str(ROOT / "output" / "work"), help="중간 파일 폴더")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--tts", choices=["edge"], default=None, help="edge: 무료 AI 음성으로 자동 내레이션")
    ap.add_argument("--voice", default="ko-KR-SunHiNeural", help="ko-KR-SunHiNeural(여) / ko-KR-InJoonNeural(남)")
    ap.add_argument("--rate", default="-5%", help="말 빠르기 (예: -10%, +0%)")
    ap.add_argument("--audio", default=None, help="직접 만든 내레이션 파일")
    ap.add_argument("--srt", default=None,
                    help="--audio와 함께: Vrew 등에서 내보낸 자막 파일. 이 시간에 맞춰 이미지를 배치하고 이 자막을 그대로 씀")
    ap.add_argument("--channel", default="", help="대본의 [채널명] 자리에 넣을 이름")
    ap.add_argument("--pad", type=float, default=0.4, help="이미지마다 음성 뒤 쉬는 시간(초)")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--font", default="Malgun Gothic",
                    help="자막 글꼴 (Windows: Malgun Gothic / Mac: Apple SD Gothic Neo / Linux: Noto Sans CJK KR)")
    ap.add_argument("--fontsize", type=int, default=20, help="자막 크기 (기본 20, 화면 높이의 약 7%%)")
    ap.add_argument("--no-burn", action="store_true", help="자막을 화면에 새기지 않고 .srt 파일로만 저장")
    ap.add_argument("--crf", type=int, default=20, help="화질 (낮을수록 고화질, 18~23)")
    args = ap.parse_args()

    all_rows = load_rows(args.csv)
    rows = [r for r in all_rows
            if r["n"] >= args.start and (args.end is None or r["n"] <= args.end)]
    if (args.audio or args.srt) and args.start != 1:
        sys.exit("--audio/--srt를 쓸 때는 1번부터 시작해야 음성과 맞습니다. (--end로 앞부분만 시험은 가능)")
    name = args.channel or "저희 채널"
    for r in all_rows:
        r["text"] = r["text"].replace("[채널명]", name)
        r["speak"] = r["text"]

    images = Path(args.images)
    missing = [r["file"] for r in rows if not (images / r["file"]).exists()]
    if missing:
        sys.exit(f"이미지 {len(missing)}장이 없습니다. 예: {', '.join(missing[:5])}\n"
                 f"먼저 generate_images.py로 이미지를 만들어 주세요. (폴더: {images})")

    work = Path(args.work)
    (work / "clips").mkdir(parents=True, exist_ok=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    # 1) 이미지별 길이 정하기
    if args.tts:
        print("① 내레이션 음성 만드는 중...")
        asyncio.run(tts_all(rows, work / "tts", args.voice, args.rate))
        for r in rows:
            r["dur"] = media_duration(work / "tts" / f"{r['n']:04d}.mp3") + args.pad
    elif args.audio and args.srt:
        # Vrew 등에서 내보낸 자막(SRT)의 시간에 맞춰 이미지 경계를 정함
        cues = parse_srt(args.srt)
        bounds = align_to_srt(all_rows, cues)  # 전체 대본 기준으로 맞춘 뒤 필요한 구간만 사용
        bounds[0] = 0.0
        if args.end is None:
            bounds[-1] = max(bounds[-1], media_duration(args.audio))
        for r, a, b in zip(rows, bounds, bounds[1:len(rows) + 1]):
            r["dur"] = max(b - a, 0.5)
    elif args.audio:
        total = media_duration(args.audio)
        est = sum(r["sec"] for r in rows)
        for r in rows:
            r["dur"] = r["sec"] * total / est
    else:
        for r in rows:
            r["dur"] = r["sec"]

    # 2) 이미지마다 켄 번스 클립 만들기
    print("② 이미지 클립 만드는 중...")
    for i, r in enumerate(rows):
        clip = work / "clips" / f"{r['n']:04d}.mp4"
        frames = max(int(round(r["dur"] * args.fps)), 1)
        r["frames"] = frames
        if clip.exists() and clip.stat().st_size > 0 and abs(media_duration(clip) - frames / args.fps) < 0.05:
            continue
        z, x, y = (s.format(N=frames) for s in MOTIONS[r["n"] % len(MOTIONS)])
        vf = ("scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,"
              f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s=1920x1080:fps={args.fps},format=yuv420p")
        run(["-i", str(images / r["file"]), "-vf", vf, "-frames:v", str(frames),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(args.crf), "-r", str(args.fps), str(clip)])
        print(f"  클립 {i + 1}/{len(rows)} ({r['file']}, {r['dur']:.1f}초)")

    # 3) 클립 이어 붙이기
    print("③ 클립 이어 붙이는 중...")
    listfile = work / "clips.txt"
    listfile.write_text("".join(f"file 'clips/{r['n']:04d}.mp4'\n" for r in rows), encoding="utf-8")
    video_only = work / "video_only.mp4"
    run(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(video_only)])

    # 4) 오디오 트랙
    audio = work / "narration.m4a"
    if args.tts:
        parts = []
        for r in rows:
            wav = work / "tts" / f"{r['n']:04d}.wav"
            run(["-i", str(work / "tts" / f"{r['n']:04d}.mp3"), "-af",
                 f"apad=whole_dur={r['frames'] / args.fps:.3f}", "-ar", "44100", "-ac", "1", str(wav)])
            parts.append(wav)
        alist = work / "audio.txt"
        alist.write_text("".join(f"file 'tts/{p.name}'\n" for p in parts), encoding="utf-8")
        run(["-f", "concat", "-safe", "0", "-i", str(alist), "-c:a", "aac", "-b:a", "192k", str(audio)])
    elif args.audio:
        run(["-i", args.audio, "-c:a", "aac", "-b:a", "192k", str(audio)])
    else:
        total = sum(r["frames"] for r in rows) / args.fps
        run(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{total:.3f}", "-c:a", "aac", str(audio)])

    # 5) 자막(SRT): 이미지 구간 안에서 문장 길이에 비례해 시간 배분
    srt_path = Path(args.out).with_suffix(".srt")
    if args.srt:
        # Vrew 자막 시간이 음성과 정확히 맞으므로 그대로 사용
        if Path(args.srt).resolve() != srt_path.resolve():
            shutil.copyfile(args.srt, srt_path)
        rows_for_srt = []
    else:
        rows_for_srt = rows
    t, idx, lines = 0.0, 1, []
    for r in rows_for_srt:
        dur = r["frames"] / args.fps
        speak = dur - (args.pad if args.tts else 0)
        sents = split_sentences(r["text"])
        total_chars = sum(len(s) for s in sents) or 1
        st = t
        for s in sents:
            d = speak * len(s) / total_chars
            lines.append(f"{idx}\n{srt_time(st)} --> {srt_time(st + d)}\n{s}\n")
            idx += 1
            st += d
        t += dur
    if rows_for_srt:
        srt_path.write_text("\n".join(lines), encoding="utf-8")

    # 6) 최종 합치기
    print("④ 최종 영상 만드는 중... (길이에 따라 시간이 걸립니다)")
    out = str(Path(args.out))
    if args.no_burn:
        run(["-i", str(video_only), "-i", str(audio), "-c:v", "copy", "-c:a", "copy", "-shortest", out])
    else:
        style = (f"FontName={args.font},FontSize={args.fontsize},PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=1,Bold=1,MarginV=28")
        srt_rel = srt_path.resolve().as_posix().replace(":", "\\:")
        run(["-i", str(video_only), "-i", str(audio), "-vf", f"subtitles='{srt_rel}':force_style='{style}'",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(args.crf), "-c:a", "copy", "-shortest", out])

    total = media_duration(out)
    print(f"\n완료: {out}  (길이 {int(total // 60)}분 {int(total % 60)}초)")
    print(f"자막 파일: {srt_path}  (유튜브 자막으로 따로 올릴 수도 있습니다)")


if __name__ == "__main__":
    main()
