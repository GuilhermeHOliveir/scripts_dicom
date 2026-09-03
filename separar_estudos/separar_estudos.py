#!/usr/bin/env python3
"""Separa series DICOM em estudos independentes com validacao transacional."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.multival import MultiValue
from pydicom.uid import (
    ExplicitVRBigEndian,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    generate_uid,
)


@dataclass(frozen=True)
class SeriesKey:
    study_uid: str
    series_uid: str


@dataclass
class InstanceInfo:
    path: Path
    key: SeriesKey
    sop_uid: str


@dataclass
class SeriesInfo:
    key: SeriesKey
    description: str
    number: str
    modality: str
    patient_name: str
    patient_id: str
    instances: List[InstanceInfo]


class SeparationError(RuntimeError):
    pass


def clean_path(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"').strip("'")))).resolve()


def is_inside(candidate: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(candidate), str(parent)]) == str(parent)
    except ValueError:
        return False


def safe_component(value: str, fallback: str = "sem_nome") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")
    value = re.sub(r"\s+", "_", value)
    return value[:80] or fallback


def uid_suffix(uid: str) -> str:
    return safe_component(uid.rsplit(".", 1)[-1], "uid")[-16:]


def pixel_hash(ds: pydicom.dataset.Dataset) -> Optional[str]:
    for keyword in ("PixelData", "FloatPixelData", "DoubleFloatPixelData"):
        if keyword in ds:
            value = ds.get(keyword)
            if isinstance(value, bytes):
                return hashlib.sha256(value).hexdigest()
            return hashlib.sha256(bytes(value)).hexdigest()
    return None


def scan_source(source: Path, force: bool = False) -> Tuple[List[SeriesInfo], List[dict]]:
    grouped: Dict[SeriesKey, SeriesInfo] = {}
    skipped: List[dict] = []

    for path in sorted((p for p in source.rglob("*") if p.is_file()), key=lambda p: str(p).lower()):
        try:
            ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=force)
        except Exception as exc:
            skipped.append({"arquivo": str(path), "motivo": "nao reconhecido como DICOM", "detalhe": str(exc)})
            continue

        missing = [name for name in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID") if not ds.get(name)]
        if missing:
            skipped.append({"arquivo": str(path), "motivo": "UID obrigatorio ausente", "detalhe": ", ".join(missing)})
            continue

        key = SeriesKey(str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID))
        if key not in grouped:
            grouped[key] = SeriesInfo(
                key=key,
                description=str(ds.get("SeriesDescription", "")),
                number=str(ds.get("SeriesNumber", "")),
                modality=str(ds.get("Modality", "")),
                patient_name=str(ds.get("PatientName", "")),
                patient_id=str(ds.get("PatientID", "")),
                instances=[],
            )
        grouped[key].instances.append(InstanceInfo(path, key, str(ds.SOPInstanceUID)))

    series = sorted(grouped.values(), key=lambda s: (s.key.study_uid, s.number, s.description, s.key.series_uid))
    return series, skipped


def print_inventory(series: Sequence[SeriesInfo], skipped_count: int) -> None:
    print("\n=== SERIES DICOM ENCONTRADAS ===")
    for index, item in enumerate(series, 1):
        print(
            "[{0:03d}] imagens={1:<5} modalidade={2:<4} numero={3:<6} descricao={4}".format(
                index, len(item.instances), item.modality or "-", item.number or "-", item.description or "-"
            )
        )
        print("      Study={}\n      Series={}".format(item.key.study_uid, item.key.series_uid))
    if skipped_count:
        print("\nAviso: {} arquivo(s) nao foram incluidos. O relatorio guardara os motivos.".format(skipped_count))


def ask_existing_directory(label: str) -> Path:
    while True:
        value = input(label).strip()
        if not value:
            print("Informe um caminho.")
            continue
        path = clean_path(value)
        if path.is_dir():
            return path
        print("A pasta nao existe ou nao e um diretorio: {}".format(path))


def ask_destination(source: Path) -> Path:
    while True:
        value = input("Pasta de destino (nova ou vazia): ").strip()
        if not value:
            print("Informe um caminho.")
            continue
        path = clean_path(value)
        if path == source or is_inside(path, source) or is_inside(source, path):
            print("Origem e destino nao podem ser iguais, ancestrais ou descendentes entre si.")
            continue
        if path.exists() and (not path.is_dir() or any(path.iterdir())):
            print("O destino precisa nao existir ou estar completamente vazio.")
            continue
        return path


def ask_assignments(series: Sequence[SeriesInfo]) -> Dict[SeriesKey, str]:
    print("\nPara cada serie, informe:")
    print("  um nome de grupo  -> gera UIDs novos; nomes iguais formam o mesmo novo estudo")
    print("  M                 -> copia preservando todos os UIDs")
    print("  I                 -> ignora a serie (padrao seguro)")
    assignments: Dict[SeriesKey, str] = {}
    for index, item in enumerate(series, 1):
        while True:
            answer = input("[{:03d}] {}: ".format(index, item.description or item.key.series_uid)).strip()
            if not answer:
                answer = "I"
            if answer.upper() == "I":
                break
            if answer.upper() == "M":
                assignments[item.key] = "__PRESERVE__"
                break
            if safe_component(answer):
                assignments[item.key] = answer
                break
    return assignments


def summarize_plan(series: Sequence[SeriesInfo], assignments: Dict[SeriesKey, str]) -> None:
    totals = defaultdict(lambda: [0, 0])
    ignored = [0, 0]
    for item in series:
        target = assignments.get(item.key)
        if target is None:
            ignored[0] += 1
            ignored[1] += len(item.instances)
        else:
            totals[target][0] += 1
            totals[target][1] += len(item.instances)
    print("\n=== PLANO ===")
    for target, (series_count, image_count) in totals.items():
        label = "preservar UIDs" if target == "__PRESERVE__" else "novo estudo '{}'".format(target)
        print("- {}: {} serie(s), {} arquivo(s)".format(label, series_count, image_count))
    print("- ignorar: {} serie(s), {} arquivo(s)".format(*ignored))


def ensure_unique_sops(series: Sequence[SeriesInfo], assignments: Dict[SeriesKey, str]) -> None:
    locations: Dict[str, List[str]] = defaultdict(list)
    for item in series:
        if item.key not in assignments:
            continue
        for instance in item.instances:
            locations[instance.sop_uid].append(str(instance.path))
    duplicates = {uid: paths for uid, paths in locations.items() if len(paths) > 1}
    if duplicates:
        sample = next(iter(duplicates.items()))
        raise SeparationError(
            "SOPInstanceUID duplicado nos arquivos selecionados: {} em {}. "
            "A operacao foi cancelada para evitar referencias ambiguas.".format(sample[0], sample[1])
        )


def validate_assignments(series: Sequence[SeriesInfo], assignments: Dict[SeriesKey, str]) -> None:
    """Impede mesclas acidentais e mapas de UID ambiguos."""
    by_group: Dict[str, List[SeriesInfo]] = defaultdict(list)
    series_uid_owners: Dict[str, Set[str]] = defaultdict(set)
    for item in series:
        target = assignments.get(item.key)
        if target is None or target == "__PRESERVE__":
            continue
        by_group[target].append(item)
        series_uid_owners[item.key.series_uid].add(item.key.study_uid)

    ambiguous = {uid: studies for uid, studies in series_uid_owners.items() if len(studies) > 1}
    if ambiguous:
        uid, studies = next(iter(ambiguous.items()))
        raise SeparationError(
            "SeriesInstanceUID {} aparece em estudos diferentes ({}); nao e seguro remapear referencias.".format(
                uid, ", ".join(sorted(studies))
            )
        )

    for group, items in by_group.items():
        studies = {item.key.study_uid for item in items}
        identities = {(item.patient_id, item.patient_name) for item in items if item.patient_id or item.patient_name}
        if len(studies) > 1:
            raise SeparationError(
                "O grupo '{}' contem series de {} estudos originais. Use nomes de grupo diferentes; "
                "esta ferramenta separa estudos, mas nao os mescla.".format(group, len(studies))
            )
        if len(identities) > 1:
            raise SeparationError(
                "O grupo '{}' apresenta identidades de paciente divergentes; operacao cancelada.".format(group)
            )


def replace_uids(ds: pydicom.dataset.Dataset, uid_map: Dict[str, str]) -> int:
    changed = 0
    for elem in ds.iterall():
        if elem.VR != "UI":
            continue
        value = elem.value
        if isinstance(value, (list, tuple, MultiValue)):
            replacement = [uid_map.get(str(item), str(item)) for item in value]
            changed += sum(str(old) != new for old, new in zip(value, replacement))
            elem.value = replacement
        elif value is not None and str(value) in uid_map:
            elem.value = uid_map[str(value)]
            changed += 1
    return changed


def sync_file_meta(ds: pydicom.dataset.Dataset) -> None:
    if not hasattr(ds, "file_meta") or ds.file_meta is None:
        ds.file_meta = FileMetaDataset()
    if ds.get("SOPClassUID"):
        ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    if not ds.file_meta.get("TransferSyntaxUID"):
        if getattr(ds, "is_implicit_VR", True):
            ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
        elif getattr(ds, "is_little_endian", True):
            ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        else:
            ds.file_meta.TransferSyntaxUID = ExplicitVRBigEndian
    if not getattr(ds, "preamble", None):
        ds.preamble = b"\0" * 128


def write_dataset(ds: pydicom.dataset.Dataset, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    ds.save_as(str(destination), write_like_original=False)


def validate_written(
    destination: Path,
    expected_study: str,
    expected_series: str,
    expected_sop: str,
    expected_pixel_hash: Optional[str],
) -> None:
    check = pydicom.dcmread(str(destination), force=False)
    actual = (
        str(check.get("StudyInstanceUID", "")),
        str(check.get("SeriesInstanceUID", "")),
        str(check.get("SOPInstanceUID", "")),
    )
    expected = (expected_study, expected_series, expected_sop)
    if actual != expected:
        raise SeparationError("UIDs divergentes apos reabrir {}: {} != {}".format(destination, actual, expected))
    meta_sop = str(check.file_meta.get("MediaStorageSOPInstanceUID", ""))
    if meta_sop != expected_sop:
        raise SeparationError("MediaStorageSOPInstanceUID inconsistente em {}".format(destination))
    if pixel_hash(check) != expected_pixel_hash:
        raise SeparationError("Pixel Data mudou durante a gravacao de {}".format(destination))


def build_output(
    source: Path,
    destination: Path,
    series: Sequence[SeriesInfo],
    assignments: Dict[SeriesKey, str],
    skipped: Sequence[dict],
    force_read: bool = False,
) -> dict:
    ensure_unique_sops(series, assignments)
    validate_assignments(series, assignments)
    transformed = [s for s in series if assignments.get(s.key) not in (None, "__PRESERVE__")]
    group_names = sorted(name for name in set(assignments.values()) if name != "__PRESERVE__")
    group_study_uid = {name: generate_uid() for name in group_names}
    group_folder_map = {
        name: "{:03d}_{}".format(index, safe_component(name, "novo_estudo"))
        for index, name in enumerate(group_names, 1)
    }
    series_uid_map = {s.key.series_uid: generate_uid() for s in transformed}
    sop_uid_map = {i.sop_uid: generate_uid() for s in transformed for i in s.instances}

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".separar_estudos_", dir=str(destination.parent)))
    report = {
        "origem": str(source),
        "destino": str(destination),
        "pydicom": pydicom.__version__,
        "arquivos_gravados": 0,
        "series": [],
        "arquivos_ignorados_na_leitura": list(skipped),
        "observacoes": [
            "O processo nao anonimiza dados do paciente.",
            "Referencias UID conhecidas nos datasets selecionados foram atualizadas.",
            "Objetos externos que nao estavam na origem nao podem ser validados.",
            "Texto eventualmente gravado nos pixels nao e detectado nem removido.",
        ],
    }

    try:
        for series_index, item in enumerate(series, 1):
            assignment = assignments.get(item.key)
            if assignment is None:
                continue
            preserve = assignment == "__PRESERVE__"
            if preserve:
                study_uid = item.key.study_uid
                series_uid = item.key.series_uid
                group_folder = "preservado_estudo_{}".format(uid_suffix(study_uid))
            else:
                study_uid = str(group_study_uid[assignment])
                series_uid = str(series_uid_map[item.key.series_uid])
                group_folder = group_folder_map[assignment]

            series_folder = "{:03d}_{}_{}".format(
                series_index, safe_component(item.description, "serie"), uid_suffix(series_uid)
            )
            entry = {
                "grupo": "preservado" if preserve else assignment,
                "descricao": item.description,
                "arquivos": len(item.instances),
                "study_uid_original": item.key.study_uid,
                "study_uid_novo": study_uid,
                "series_uid_original": item.key.series_uid,
                "series_uid_novo": series_uid,
                "uids_preservados": preserve,
            }
            report["series"].append(entry)

            for instance_index, instance in enumerate(item.instances, 1):
                if preserve:
                    sop_uid = instance.sop_uid
                    output = staging / group_folder / series_folder / "{:06d}_{}.dcm".format(
                        instance_index, uid_suffix(sop_uid)
                    )
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(instance.path), str(output))
                    original = pydicom.dcmread(str(instance.path), force=force_read)
                    validate_written(output, study_uid, series_uid, sop_uid, pixel_hash(original))
                else:
                    ds = pydicom.dcmread(str(instance.path), force=force_read)
                    original_pixel_hash = pixel_hash(ds)
                    uid_map = dict(sop_uid_map)
                    uid_map.update(series_uid_map)
                    uid_map[item.key.study_uid] = study_uid
                    replace_uids(ds, uid_map)
                    ds.StudyInstanceUID = study_uid
                    ds.SeriesInstanceUID = series_uid
                    ds.SOPInstanceUID = sop_uid_map[instance.sop_uid]
                    sync_file_meta(ds)
                    sop_uid = str(ds.SOPInstanceUID)
                    output = staging / group_folder / series_folder / "{:06d}_{}.dcm".format(
                        instance_index, uid_suffix(sop_uid)
                    )
                    write_dataset(ds, output)
                    validate_written(output, study_uid, series_uid, sop_uid, original_pixel_hash)
                report["arquivos_gravados"] += 1

        with (staging / "relatorio_separacao.json").open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)

        expected = sum(len(s.instances) for s in series if s.key in assignments)
        if report["arquivos_gravados"] != expected:
            raise SeparationError("Quantidade final divergente: {} != {}".format(report["arquivos_gravados"], expected))

        if destination.exists():
            destination.rmdir()  # somente destino previamente confirmado como vazio
        staging.replace(destination)
        return report
    except Exception:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inventaria e separa series DICOM em novos estudos.")
    parser.add_argument("--origem", help="Pasta contendo os arquivos DICOM")
    parser.add_argument("--destino", help="Pasta de saida, que deve ser nova ou vazia")
    parser.add_argument(
        "--force-scan",
        action="store_true",
        help="Aceita no inventario arquivos DICOM sem preambulo/cabecalho padrao (use somente se necessario)",
    )
    parser.add_argument("--inventario", action="store_true", help="Apenas lista as series, sem gravar arquivos")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    print("=== SEPARADOR SEGURO DE ESTUDOS DICOM ===")
    source = clean_path(args.origem) if args.origem else ask_existing_directory("Pasta de origem dos DICOM: ")
    if not source.is_dir():
        print("ERRO: origem inexistente: {}".format(source), file=sys.stderr)
        return 2

    print("Analisando {} ...".format(source))
    series, skipped = scan_source(source, force=args.force_scan)
    if not series:
        print("ERRO: nenhuma serie DICOM valida foi encontrada.", file=sys.stderr)
        return 2
    print_inventory(series, len(skipped))
    if args.inventario:
        return 0

    destination = clean_path(args.destino) if args.destino else ask_destination(source)
    if destination == source or is_inside(destination, source) or is_inside(source, destination):
        print("ERRO: origem e destino nao podem conter um ao outro.", file=sys.stderr)
        return 2
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        print("ERRO: o destino precisa nao existir ou estar vazio.", file=sys.stderr)
        return 2

    assignments = ask_assignments(series)
    if not assignments:
        print("Nenhuma serie foi selecionada; nada foi alterado.")
        return 0
    summarize_plan(series, assignments)
    confirmation = input("Digite SEPARAR para executar: ").strip()
    if confirmation != "SEPARAR":
        print("Operacao cancelada; nada foi gravado.")
        return 0

    try:
        report = build_output(source, destination, series, assignments, skipped, force_read=args.force_scan)
    except Exception as exc:
        print("ERRO: operacao cancelada e area temporaria removida: {}".format(exc), file=sys.stderr)
        return 1

    print("\nConcluido e validado: {} arquivo(s).".format(report["arquivos_gravados"]))
    print("Saida: {}".format(destination))
    print("Relatorio: {}".format(destination / "relatorio_separacao.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
