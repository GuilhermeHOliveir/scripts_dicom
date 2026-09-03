import tempfile
import unittest
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from separar_estudos import SeriesKey, build_output, scan_source


def create_dicom(path: Path, study_uid: str, series_uid: str, sop_uid: str, description: str) -> None:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.is_little_endian = True
    ds.is_implicit_VR = False
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientName = "PACIENTE^TESTE"
    ds.PatientID = "123"
    ds.Modality = "CT"
    ds.SeriesNumber = 1
    ds.SeriesDescription = description
    ds.Rows = 1
    ds.Columns = 2
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = b"\x01\x02"
    reference = Dataset()
    reference.ReferencedSOPInstanceUID = sop_uid
    reference.ReferencedSOPClassUID = CTImageStorage
    ds.ReferencedImageSequence = [reference]
    ds.save_as(str(path), write_like_original=False)


class SeparationTests(unittest.TestCase):
    def test_new_study_updates_uids_meta_references_and_pixels(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            destination = base / "output"
            study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
            create_dicom(source / "image.dcm", study_uid, series_uid, sop_uid, "TORAX")

            series, skipped = scan_source(source)
            self.assertEqual(len(series), 1)
            self.assertFalse(skipped)
            report = build_output(source, destination, series, {series[0].key: "torax"}, skipped)

            outputs = list(destination.rglob("*.dcm"))
            self.assertEqual(report["arquivos_gravados"], 1)
            self.assertEqual(len(outputs), 1)
            ds = pydicom.dcmread(str(outputs[0]))
            self.assertNotEqual(str(ds.StudyInstanceUID), study_uid)
            self.assertNotEqual(str(ds.SeriesInstanceUID), series_uid)
            self.assertNotEqual(str(ds.SOPInstanceUID), sop_uid)
            self.assertEqual(str(ds.file_meta.MediaStorageSOPInstanceUID), str(ds.SOPInstanceUID))
            self.assertEqual(str(ds.ReferencedImageSequence[0].ReferencedSOPInstanceUID), str(ds.SOPInstanceUID))
            self.assertEqual(ds.PixelData, b"\x01\x02")

    def test_preserve_copies_without_uid_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            destination = base / "output"
            study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
            create_dicom(source / "image.dcm", study_uid, series_uid, sop_uid, "CRANIO")
            series, skipped = scan_source(source)

            build_output(source, destination, series, {series[0].key: "__PRESERVE__"}, skipped)
            ds = pydicom.dcmread(str(next(destination.rglob("*.dcm"))))
            self.assertEqual(str(ds.StudyInstanceUID), study_uid)
            self.assertEqual(str(ds.SeriesInstanceUID), series_uid)
            self.assertEqual(str(ds.SOPInstanceUID), sop_uid)

    def test_duplicate_sop_aborts_without_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            destination = base / "output"
            study_uid, series_uid, sop_uid = generate_uid(), generate_uid(), generate_uid()
            create_dicom(source / "one.dcm", study_uid, series_uid, sop_uid, "A")
            create_dicom(source / "two.dcm", study_uid, series_uid, sop_uid, "A")
            series, skipped = scan_source(source)

            with self.assertRaises(Exception):
                build_output(source, destination, series, {SeriesKey(study_uid, series_uid): "grupo"}, skipped)
            self.assertFalse(destination.exists())

    def test_refuses_to_merge_different_original_studies(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source"
            source.mkdir()
            destination = base / "output"
            study_one, study_two = generate_uid(), generate_uid()
            create_dicom(source / "one.dcm", study_one, generate_uid(), generate_uid(), "A")
            create_dicom(source / "two.dcm", study_two, generate_uid(), generate_uid(), "B")
            series, skipped = scan_source(source)
            assignments = {item.key: "mesmo_grupo" for item in series}

            with self.assertRaises(Exception):
                build_output(source, destination, series, assignments, skipped)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
