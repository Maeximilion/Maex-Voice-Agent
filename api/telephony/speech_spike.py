"""Kit of the spike T-10.3: pick the open ear and mouth of the test phase.

    python3 -m scripts.speech_spike [--dry-run] [--limit N] [--run-dir DIR]
                                    [--menu FILE] [--judge-only]

    --dry-run   check the environment and print every step, change nothing
    --limit     only the first N number sentences and the first N dish names:
                a trial of the kit, not a measurement
    --run-dir   where models, samples and results go
                (default deploy/speech/run, ignored by git)
    --menu      a `menu_items.csv` in the import format (docs/14) from outside
                the repository instead of the eval menu: its dish names are
                spoken, every one of its card numbers is spoken once ("Nummer
                S12"), and what is heard is judged against its numbers. A real
                menu stays out of the repository; its names end up in the run
                directory only (plan, results, the list of wrong numbers), so
                with --menu that directory is the default or outside the
                repository
    --judge-only
                judge the recorded run in --run-dir again and print its report:
                the same plan and transcripts, the number rule as it is now.
                Speaks and hears nothing; for a change to the number rule

Builds the image of `deploy/speech/` (the open speech engines next to the
project's packages) and runs three containers from it, one after another: one
downloads the model files (the only step with network access; about 5 GB on the
first run, kept in the run directory), one speaks the sentences of the number
eval and the dish names of the eval menu with every voice, sends them through
the band of a telephone line and lets every ear transcribe them
(`speech_bench.py`), one judges what was heard with the rule that resolves
numbers on the phone (`evals/heard.py`). The last two run without a network: no
audio and no sentence leaves the machine. Prints one table for the ears, one
for the mouths, one for the silence detector, the pick or the ear that came
closest, and writes
`report.json` and one sample per voice and agent sentence (`samples/`) for the
human ear.

Needs Docker and Python 3.12, nothing else on the host; no sudo and not root.
A graphics card is used when Docker hands one to a container; without one the
ears that need it are listed as failed and the others are measured. Invented
sentences only.

Which engines are tried is the table below, not a setting: this is a spike.
Lives in `telephony/` because it names the speech engines; `scripts/speech_spike.py`
is its entry (docs/11 §telephony).

Exit codes: 0 an ear within the threshold for silent wrong numbers, 1 none (the
tables and the closest ear are printed all the same) or a step broke, 2 wrong
call or Docker not usable.
"""

import argparse
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from api.telephony.spike import SpikeError, docker_problem, step, usable_run_dir

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEECH_DIR = REPO_ROOT / "deploy" / "speech"
NUMBERS = REPO_ROOT / "evals" / "cases" / "nummern.jsonl"
MENU = REPO_ROOT / "evals" / "menu" / "menu_items.csv"
IMAGE = "maex-speech-spike"
RUN_MOUNT = "/work"
RESULTS = ("plan.json", "heard.json", "judged.json", "report.json")
# Its own marker: the run directory of the T-10.1 kit is not one of this kit's.
RUN_MARKER = ".speech-spike-run"

# The candidates. A second line for the same model is the same ear with the
# key terms of the menu handed to it. An ear with `sample` hears only about
# that many utterances: it is timed, and never picked.
EARS: tuple[dict[str, Any], ...] = (
    {
        "id": "whisper-turbo-gpu",
        "engine": "whisper",
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute": "float16",
    },
    {
        "id": "whisper-turbo-gpu-terms",
        "engine": "whisper",
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute": "float16",
        "key_terms": True,
    },
    {
        "id": "whisper-turbo-cpu",
        "engine": "whisper",
        "model": "large-v3-turbo",
        "device": "cpu",
        "compute": "int8",
        # The same weights as on the graphics card, so only its speed is
        # new, and that takes seconds per sentence.
        "sample": 40,
    },
    {
        "id": "whisper-small-cpu",
        "engine": "whisper",
        "model": "small",
        "device": "cpu",
        "compute": "int8",
    },
    {
        "id": "parakeet-v3-cpu",
        "engine": "parakeet",
        "model": "nemo-parakeet-tdt-0.6b-v3",
        "repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "files": [
            "config.json",
            "vocab.txt",
            "nemo128.onnx",
            "encoder-model.onnx*",
            "decoder_joint-model.onnx",
        ],
    },
)
# Every voice is a candidate for the mouth and one of the callers the ears hear.
VOICES = (
    "de_DE-thorsten-medium",
    "de_DE-thorsten-high",
    "de_DE-kerstin-low",
    "de_DE-karlsson-low",
)
# What the mouth is timed on and what the samples say. Invented.
AGENT_SENTENCES = (
    "Guten Tag, hier ist der digitale Assistent des Restaurants. "
    "Was darf ich für Sie tun?",
    "Ich wiederhole: zweimal die Nummer dreiundzwanzig und einmal Pho Bo, "
    "zur Abholung um halb sieben. Stimmt das so?",
    "Für wie viele Personen darf ich reservieren?",
)
SILENCES_MS = (300, 500, 700)

# Thresholds of docs/20 §6.
WRONG_LIMIT = 0.02
VOICE_TO_VOICE_SECONDS = 1.5


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.speech_spike", description=__doc__
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--run-dir", type=Path, default=SPEECH_DIR / "run")
    parser.add_argument("--menu", type=Path)
    parser.add_argument("--judge-only", action="store_true")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    recorded = args.judge_only

    if args.limit is not None and args.limit < 1:
        parser.error("--limit needs a number above zero")
    if recorded and (args.limit is not None or args.menu):
        parser.error("--judge-only takes the plan of the run: no --limit, no --menu")
    own_run = (SPEECH_DIR / "run").resolve()
    if args.menu and run_dir != own_run and run_dir.is_relative_to(REPO_ROOT):
        # Plan, transcripts and report name the dishes of that menu. Inside the
        # repository git ignores only the kit's own run folder (Codex PR #268).
        print(
            f"speech_spike: with --menu the run directory holds the menu's dish "
            f"names, and {run_dir} is a folder git could track. Leave --run-dir "
            "out or name a folder outside the repository",
            file=sys.stderr,
        )
        return 2
    if os.getuid() == 0:
        print(
            "speech_spike: run it as an unprivileged user. The containers take the "
            "caller's uid, as root they would run as root",
            file=sys.stderr,
        )
        return 2
    if not usable_run_dir(run_dir, RUN_MARKER):
        print(
            f"speech_spike: {run_dir} is not a run directory of this kit (it holds "
            f"files and no {RUN_MARKER}). The kit clears its results there: name an "
            "empty or a new one",
            file=sys.stderr,
        )
        return 2
    problem = docker_problem()
    if problem:
        print(f"speech_spike: {problem}", file=sys.stderr)
        return 2

    try:
        if recorded:
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            (run_dir / "heard.json").stat()
        else:
            plan = build_plan(args.limit, args.menu)
    except (OSError, KeyError, ValueError) as exc:
        problem = (
            f"--judge-only needs plan.json and heard.json of a run in {run_dir}"
            if recorded
            else f"--menu {args.menu} is no menu in the import format (columns "
            "number, name and category, separated by ';')"
        )
        print(f"speech_spike: {problem}: {exc!r}", file=sys.stderr)
        return 2
    dry = args.dry_run
    try:
        if recorded and not dry:
            # The verdict of the run before must not outlive a judge that
            # breaks: exit 1 is also what a no-go returns (Codex PR #268).
            for name in ("judged.json", "report.json"):
                (run_dir / name).unlink(missing_ok=True)
        if not recorded:
            step("Write the plan into", run_dir)
            if not dry:
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / RUN_MARKER).touch()
                clear_results(run_dir)
                (run_dir / "plan.json").write_text(
                    json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
                )
        step("Build the image with the speech engines")
        build = ["docker", "build", "-t", IMAGE]
        docker([*build, "-f", "Dockerfile", "../../api"], dry, cwd=SPEECH_DIR)
        if not recorded:
            gpu = not dry and gpu_in_docker()
            step("Download the model files (network; only what is missing)")
            bench = "api.telephony.speech_bench"
            docker(in_image(run_dir, bench, "fetch", network=True), dry)
            step(
                f"Speak and hear: {len(plan['sentences'])} sentences, "
                f"{len(VOICES)} voices, {len(EARS)} ears (no network)"
            )
            docker(in_image(run_dir, bench, "measure", gpu=gpu), dry)
        step("Judge what was heard (no network)")
        docker(in_image(run_dir, "evals.heard"), dry)
    except SpikeError as exc:
        print(f"speech_spike: {exc}", file=sys.stderr)
        return 1
    if dry:
        return 0

    heard = json.loads((run_dir / "heard.json").read_text(encoding="utf-8"))
    judged = json.loads((run_dir / "judged.json").read_text(encoding="utf-8"))
    report = summarize(plan, heard, judged)
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print_report(report, run_dir)
    return 0 if report["go"] else 1


def build_plan(limit: int | None, menu: Path | None = None) -> dict[str, Any]:
    """Sentences and key terms from the eval files, never from this module.

    `menu`: another menu than the eval menu (`--menu`). Its dish names and card
    numbers are spoken, and the plan carries its numbers for the judge. The
    expectations of the number eval were written for the eval menu, so with
    another one they are left to the judge (`evals/heard.py`).
    """
    numbers = [
        json.loads(line)
        for raw in NUMBERS.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    ]
    with (menu or MENU).open(encoding="utf-8-sig", newline="") as f:
        dishes = [
            {key: row[key].strip() for key in ("number", "name", "category")}
            for row in csv.DictReader(f, delimiter=";")
            # As the import reads the flag (`importer._bool`): " NEIN " is no.
            if (row.get("active") or "").strip().lower() != "nein"
        ]
    if not dishes:
        raise ValueError("no active dish")
    seen: dict[str, str] = {}
    for dish in dishes:
        # The import refuses "7" next to "07"; spoken and counted twice, that
        # position would weigh double in every share (Codex PR #268).
        twin = seen.setdefault(canonical(dish["number"]), dish["number"])
        if twin is not dish["number"]:
            raise ValueError(f"card number {dish['number']} twice (also {twin})")
    # "Gyoza (6 Stück)": nobody says the bracket on the phone.
    names = [dish["name"].split(" (")[0] for dish in dishes]
    sentences = [
        {
            "id": f"n{i}",
            "kind": "number",
            "say": row["say"],
            "expect": None if menu else row["expect"],
        }
        for i, row in enumerate(numbers)
    ][:limit]
    cards = [
        {
            "id": f"c{i}",
            "kind": "number",
            "say": f"Nummer {d['number']}",
            "expect": None,
        }
        for i, d in enumerate(dishes)
        if menu
    ]
    sentences += cards[:limit]
    sentences += [
        {
            "id": f"d{i}",
            "kind": "dish",
            "say": f"Ich hätte gern {name}.",
            "expect": name,
        }
        for i, name in enumerate(names)
    ][:limit]
    plan = {
        "sentences": sentences,
        "all_sentences": len(numbers) + len(cards) + len(names),
        "key_terms": names,
        "voices": list(VOICES),
        "ears": list(EARS),
        "agent": list(AGENT_SENTENCES),
        "silences_ms": list(SILENCES_MS),
    }
    # The menu the run is judged against travels with it: a recorded run is
    # judged again (--judge-only) with the numbers it was recorded with, also
    # when the eval menu has changed since (Codex PR #268).
    plan["menu"] = [{k: d[k] for k in ("number", "category")} for d in dishes]
    plan["own_menu"] = menu is not None
    return plan


def terms_that_fit(tokens: Any, terms: list[str], limit: int) -> list[str]:
    """The leading key terms whose list stays under an ear's limit of tokens;
    `tokens` counts them for a text. The engine cuts a prompt that is too long
    without a word, in the middle of a term; cut at a whole term, the run knows
    which dishes were heard with theirs (`speech_bench.load_ear`)."""
    kept: list[str] = []
    for term in terms:
        if tokens(", ".join([*kept, term])) >= limit:
            break
        kept.append(term)
    return kept


_LEADING_ZEROS = re.compile(r"^([a-z]*)0*(\d)")


def canonical(card: str) -> str:
    """A card number as the search compares it: `numberwords.canonical_card`,
    which the host cannot import (its package needs the database driver). A
    test holds the two together."""
    return _LEADING_ZEROS.sub(r"\1\2", card.strip().lower()) or "0"


def gpu_in_docker() -> bool:
    """Whether a container of the image gets the graphics card. Asked of Docker,
    not of the host: a driver on the host is no GPU in a container, and
    `--gpus` on a Docker without one fails the whole step."""
    probe = ["docker", "run", "--rm", "--gpus", "all", IMAGE, "nvidia-smi"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def clear_results(run_dir: Path) -> None:
    """Remove what an earlier run reported. The model files stay."""
    shutil.rmtree(run_dir / "samples", ignore_errors=True)
    for name in RESULTS:
        (run_dir / name).unlink(missing_ok=True)


def docker(command: list[str], dry_run: bool, cwd: Path | None = None) -> None:
    if dry_run:
        print("   [dry-run]", " ".join(command))
        return
    # Not captured: a run takes minutes and prints where it is.
    if subprocess.run(command, cwd=cwd, check=False).returncode != 0:
        raise SpikeError(f"`{' '.join(command)}` failed")


def in_image(
    run_dir: Path, module: str, *args: str, network: bool = False, gpu: bool = False
) -> list[str]:
    """The command that runs a module of this repository in the speech image."""
    command = ["docker", "run", "--rm"]
    command += ["--gpus", "all"] if gpu else []
    command += [] if network else ["--network", "none", "-e", "HF_HUB_OFFLINE=1"]
    command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    command += ["-e", "HOME=/tmp"]
    for folder in ("api", "evals"):
        command += ["-v", f"{REPO_ROOT / folder}:/app/{folder}:ro"]
    command += ["-v", f"{run_dir}:{RUN_MOUNT}"]
    return [*command, IMAGE, "python", "-m", module, *args, RUN_MOUNT]


def summarize(
    plan: dict[str, Any], heard: dict[str, Any], judged: list[dict[str, Any]]
) -> dict[str, Any]:
    ears = [
        _ear(
            spec["id"],
            heard["ears"].get(spec["id"], {"error": "not run"}),
            judged,
            len(plan.get("key_terms", [])),
        )
        for spec in plan["ears"]
    ]
    sampled = {spec["id"] for spec in plan["ears"] if "sample" in spec}
    candidates = [e for e in ears if "error" not in e and e["id"] not in sampled]
    within = [e for e in candidates if e["wrong_share"] <= WRONG_LIMIT]
    # The most numbers understood; among equals the faster ear.
    ear = max(
        within, key=lambda e: (e["right_share"], -e["median_seconds"]), default=None
    )
    # Without a pick the tables are read by the ear that came closest.
    best = ear or min(candidates, key=lambda e: e["wrong_share"], default=None)

    mouths = []
    for voice in plan["voices"]:
        timed = [m for m in heard["mouths"] if m["voice"] == voice]
        rows = [
            r
            for r in judged
            if best
            and r["ear"] == best["id"]
            and r["voice"] == voice
            and r["kind"] == "number"
        ]
        mouths.append(
            {
                "voice": voice,
                "first_audio_seconds": statistics.median(
                    m["first_audio_seconds"] for m in timed
                ),
                "times_real_time": round(
                    sum(m["audio_seconds"] for m in timed)
                    / sum(m["seconds"] for m in timed),
                    1,
                ),
                "understood_share": _share(rows, "right"),
            }
        )
    # The voice that ear understands best; among equals the faster one.
    mouth = (
        max(mouths, key=lambda m: (m["understood_share"], -m["first_audio_seconds"]))
        if best
        else None
    )

    turn_end = []
    for ms in plan["silences_ms"]:
        rows = [r for r in heard["turn_end"] if r["silence_ms"] == ms]
        turn_end.append(
            {
                "silence_ms": ms,
                "one_turn_share": _part(rows, lambda r: r["parts"] == 1),
                "cut_share": _part(rows, lambda r: r["parts"] > 1),
                "no_speech_share": _part(rows, lambda r: r["parts"] == 0),
            }
        )

    report: dict[str, Any] = {
        "sentences": len(plan["sentences"]),
        "all_sentences": plan["all_sentences"],
        # 0: the eval menu. Else the dishes of the menu given with --menu.
        "own_menu_dishes": len(plan["menu"]) if plan.get("own_menu") else 0,
        "ears": ears,
        "mouths": mouths,
        "turn_end": turn_end,
        # `ear` and `mouth` are the best of the run; `go` says whether the ear
        # is within the limit, so whether they are a pick.
        "ear": best and best["id"],
        "mouth": mouth and mouth["voice"],
        "go": ear is not None,
    }
    if best and mouth:
        ear_and_mouth = best["median_seconds"] + mouth["first_audio_seconds"]
        report["ear_and_mouth_seconds"] = round(ear_and_mouth, 2)
        report["left_for_silence_and_model_seconds"] = round(
            VOICE_TO_VOICE_SECONDS - ear_and_mouth, 2
        )
        report["wrong"] = [
            {k: r[k] for k in ("say", "voice", "text", "expect", "got", "on_card")}
            for r in judged
            if r["ear"] == best["id"] and r["verdict"] == "wrong"
        ]
    return report


def _ear(
    ear_id: str, info: dict[str, Any], judged: list[dict[str, Any]], given: int = 0
) -> dict[str, Any]:
    rows = [r for r in judged if r["ear"] == ear_id]
    numbers = [r for r in rows if r["kind"] == "number"]
    if "error" in info or not numbers:
        return {"id": ear_id, "error": info.get("error", "heard no number sentence")}
    seconds = sorted(r["seconds"] for r in rows)
    terms: dict[str, Any] = {}
    if info.get("key_terms") is not None:
        # Dish d<i> is the i-th key term: only the leading ones fit the ear's
        # prompt, so the others were heard without their term (Codex PR #268).
        dishes = [r for r in rows if r["kind"] == "dish"]
        fit = [r for r in dishes if int(r["id"][1:]) < info["key_terms"]]
        rest = [r for r in dishes if int(r["id"][1:]) >= info["key_terms"]]
        terms = {
            "key_terms": info["key_terms"],
            "key_terms_given": given,
            "dishes_with_term_share": _share(fit, "right"),
            "dishes_without_term_share": _share(rest, "right"),
        }
    return {
        "id": ear_id,
        **terms,
        "utterances": len(rows),
        "right_share": _share(numbers, "right"),
        "asked_share": _share(numbers, "asked"),
        "wrong_share": _share(numbers, "wrong"),
        "wrong_on_card_share": _part(numbers, lambda r: r["on_card"]),
        "dishes_share": _share([r for r in rows if r["kind"] == "dish"], "right"),
        "median_seconds": statistics.median(seconds),
        "p95_seconds": seconds[int(0.95 * (len(seconds) - 1))],
        "load_seconds": info.get("load_seconds"),
        "gpu_mib": info.get("gpu_mib"),
        "by_kind": {
            kind: {
                "sentences": len(of_kind),
                **{v: _share(of_kind, v) for v in ("right", "asked", "wrong")},
            }
            for kind in NUMBER_KINDS
            if (of_kind := [r for r in numbers if number_kind(r["expect"]) == kind])
        },
    }


NUMBER_KINDS = ("plain number", "number with a letter", "no card number")


def number_kind(expect: str) -> str:
    """What a sentence of the number eval asks of the ear, from its expectation
    (`evals/number_eval.py`): "23", "s12" or "25g", and everything that must
    not become a number ("?", "name", "!23h")."""
    if expect in ("?", "name") or expect.startswith("!"):
        return NUMBER_KINDS[2]
    return NUMBER_KINDS[0] if expect.isdigit() else NUMBER_KINDS[1]


def _share(rows: list[dict[str, Any]], verdict: str) -> float:
    return _part(rows, lambda r: r["verdict"] == verdict)


def _part(rows: list[dict[str, Any]], wanted: Any) -> float:
    return round(sum(1 for r in rows if wanted(r)) / len(rows), 4) if rows else 0.0


def print_report(report: dict[str, Any], run_dir: Path) -> None:
    def pct(share: float) -> str:
        return f"{100 * share:5.1f} %"

    own = report.get("own_menu_dishes")
    which = "the menu given with --menu" if own else "the eval menu"
    menu = f"{which}, {own} dishes" if own else which
    step(
        "Ears: card numbers by the number rule (wrong: a number the guest did not "
        f"say, taken without a question; on card: of these, a dish of {menu}), "
        "dish names letter by letter"
    )
    print(
        f"{'ear':<26}{'heard':>6}{'right':>9}{'asked':>9}{'wrong':>9}{'on card':>9}{'dishes':>9}"
        f"{'median s':>10}{'p95 s':>8}{'load s':>8}{'GPU MiB':>9}"
    )
    for e in report["ears"]:
        if "error" in e:
            print(f"{e['id']:<26}failed: {e['error']}")
            continue
        print(
            f"{e['id']:<26}{e['utterances']:>6}{pct(e['right_share']):>9}"
            f"{pct(e['asked_share']):>9}"
            f"{pct(e['wrong_share']):>9}{pct(e['wrong_on_card_share']):>9}"
            f"{pct(e['dishes_share']):>9}"
            f"{e['median_seconds']:>10.2f}{e['p95_seconds']:>8.2f}"
            f"{e['load_seconds']:>8}{e['gpu_mib'] if e['gpu_mib'] is not None else '-':>9}"
        )
    for e in report["ears"]:
        if e.get("key_terms") is not None:
            print(
                f"{e['id']}: {e['key_terms']} of {e['key_terms_given']} dish names fit "
                f"the ear's prompt as key terms; dish names right with their term "
                f"{pct(e['dishes_with_term_share']).strip()}, without "
                f"{pct(e['dishes_without_term_share']).strip()}"
            )
    step("Mouths: speed, and card numbers the best ear understood from the voice")
    print(f"{'voice':<26}{'first audio s':>14}{'x real time':>13}{'understood':>12}")
    for m in report["mouths"]:
        print(
            f"{m['voice']:<26}{m['first_audio_seconds']:>14.2f}"
            f"{m['times_real_time']:>13}{pct(m['understood_share']):>12}"
        )
    step("Turn end by a silence detector, per silence that ends the turn")
    print(f"{'silence ms':<12}{'one turn':>10}{'cut in two':>12}{'no speech':>11}")
    for t in report["turn_end"]:
        print(
            f"{t['silence_ms']:<12}{pct(t['one_turn_share']):>10}"
            f"{pct(t['cut_share']):>12}{pct(t['no_speech_share']):>11}"
        )
    step("Pick")
    if report["sentences"] < report["all_sentences"]:
        print(
            f"TRIAL: {report['sentences']} of {report['all_sentences']} sentences, "
            "not a measurement"
        )
    if report["ear"] is None:
        print("no-go: no ear was measured")
        return
    best = next(e for e in report["ears"] if e["id"] == report["ear"])
    limit = pct(WRONG_LIMIT).strip()
    if report["go"]:
        print(f"ear   {best['id']}")
    else:
        print(
            f"no-go: no ear with at most {limit} silent wrong numbers. "
            f"Closest: {best['id']} with {pct(best['wrong_share']).strip()}"
        )
    for kind, k in best["by_kind"].items():
        print(
            f"      {kind:<22}{k['sentences']:>5} heard: right {pct(k['right']).strip()}, "
            f"asked {pct(k['asked']).strip()}, wrong {pct(k['wrong']).strip()}"
        )
    print(f"mouth {report['mouth']} (samples to listen to: {run_dir / 'samples'})")
    print(
        f"ear plus mouth take {report['ear_and_mouth_seconds']} s of the "
        f"{VOICE_TO_VOICE_SECONDS} s from voice to voice; "
        f"{report['left_for_silence_and_model_seconds']} s are left for the silence "
        "that ends a turn and for the text model, which this kit does not measure"
    )
    for w in report["wrong"]:
        where = "another dish" if w["on_card"] else f"not on {which}"
        print(
            f"silent wrong: said {w['say']!r} ({w['voice']}), heard {w['text']!r}, "
            f"taken as {w['got']} instead of {w['expect']} ({where})"
        )


if __name__ == "__main__":
    sys.exit(main())
