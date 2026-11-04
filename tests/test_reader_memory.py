"""Memory bounds must not change grading, QR identity, or essay crops."""
from unittest.mock import patch

import cv2
import numpy as np
import pytest
from PIL import Image

from omr.generator import generate, make_qr_image, render_sheet_image
from omr.layout import SheetConfig, layout_fingerprint
from omr.reader import MAX_IMAGE_PIXELS, QR_MAX_SIDE, _load_gray, _read_qr, read_omr
from omr.simulate import simulate_marked


@pytest.mark.parametrize('extension', ['png', 'jpg'])
def test_gray_loader_preserves_exact_grading_pixels_and_releases_color(tmp_path, extension):
    import weakref

    path = tmp_path / f'scan.{extension}'
    pixels = np.random.default_rng(17).integers(0, 256, (64, 80, 3), dtype=np.uint8)
    assert cv2.imwrite(str(path), pixels)
    original_read = cv2.imread
    expected = cv2.cvtColor(original_read(str(path)), cv2.COLOR_BGR2GRAY)
    colors = []

    def load(*args, **kwargs):
        color = original_read(*args, **kwargs)
        colors.append(weakref.ref(color))
        return color

    with patch('omr.reader.cv2.imread', side_effect=load):
        gray = _load_gray(str(path))
    assert np.array_equal(gray, expected)
    assert colors[0]() is None


def test_oversize_image_is_rejected_before_native_decode():
    with patch('omr.reader.Image.open') as header, patch('omr.reader.cv2.imread') as load:
        header.return_value.__enter__.return_value.size = (8000, 8000)
        assert 8000 * 8000 > MAX_IMAGE_PIXELS
        with pytest.raises(ValueError, match='너무 큽니다'):
            _load_gray('oversize.png')
        load.assert_not_called()


@pytest.mark.parametrize('shape', [(50, 5000), (5000, 50), (100, 200)])
def test_qr_native_detector_receives_bounded_pixels(shape):
    with patch('omr.reader.cv2.QRCodeDetector') as detector:
        detector.return_value.detectAndDecode.return_value = ('EXAM|1234|hash', None, None)
        assert _read_qr(np.zeros(shape, np.uint8)) == 'EXAM|1234|hash'
        image = detector.return_value.detectAndDecode.call_args.args[0]
        assert max(image.shape) <= QR_MAX_SIDE
        if max(shape) <= QR_MAX_SIDE:
            assert image.shape == shape


@pytest.mark.parametrize('style,extension', [('basic', 'png'), ('exam', 'jpg')])
def test_high_resolution_identity_answers_and_crops_are_preserved(tmp_path, style, extension):
    cfg = SheetConfig(exam_id='HIGHRES', num_questions=20, id_digits=5,
                      questions_per_column=10, style=style,
                      essay_count=2 if style == 'exam' else 0)
    result = generate(cfg, str(tmp_path), dpi=200, make_preview=False,
                      students=[{'id': '12345', 'name': 'Memory test'}])
    answers = {q: q % 5 + 1 for q in range(1, 21)}
    # The simulator marks the full-resolution sheet and includes the same QR.
    with patch('omr.simulate.render_sheet_image', side_effect=lambda layout, dpi:
               render_sheet_image(layout, dpi=dpi, student={'id': '12345'})):
        scan = simulate_marked(cfg, answers, '12345', dpi=200, distort=True, seed=13)
    scan = cv2.resize(scan, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    # Exercise orientation detection on the landscape exam sheet too.
    if style == 'exam':
        scan = cv2.rotate(scan, cv2.ROTATE_90_CLOCKWISE)
    path = tmp_path / f'scan.{extension}'
    assert cv2.imwrite(str(path), scan)
    del scan
    read = read_omr(str(path), result['template'], make_preview=True, make_essay_crops=True)
    assert read.answers() == answers
    assert read.student_id_qr == '12345'
    assert read.student_id_bubbles == '12345'
    assert read.layout_fingerprint == layout_fingerprint(cfg)
    assert read.exam_id == 'HIGHRES'
    assert read.preview_jpeg
    assert set(read.essay_crops) == ({21, 22} if style == 'exam' else set())


def test_dense_qr_retries_bounded_full_resolution_crop():
    """Thumbnail can locate a QR but lose modules needed for identity decoding."""
    data = ('abcdefghijklmnopqrstuvwxyz0123456789' * 2) + '|12345|74a38f7c85'
    # A 300dpi A4 landscape scan with an 18mm QR, as used by exam sheets.
    gray = np.full((2480, 3508), 255, np.uint8)
    with make_qr_image(data) as qr:
        gray[100:313, 3150:3363] = np.asarray(
            qr.resize((213, 213), resample=Image.Resampling.NEAREST))
    detector = cv2.QRCodeDetector()
    assert detector.detectAndDecode(gray)[0] == data
    scale = QR_MAX_SIDE / max(gray.shape)
    thumbnail = cv2.resize(
        gray, (round(gray.shape[1] * scale), round(gray.shape[0] * scale)),
        interpolation=cv2.INTER_AREA,
    )
    _, points, _ = detector.detectAndDecode(thumbnail)
    assert points is not None
    inputs = []

    def decode(image):
        inputs.append(image.shape)
        # Force the fallback even if a future OpenCV can decode the thumbnail.
        if len(inputs) == 1:
            return '', points, None
        return detector.detectAndDecode(image)

    with patch('omr.reader.cv2.QRCodeDetector') as factory:
        factory.return_value.detectAndDecode.side_effect = decode
        assert _read_qr(gray) == data
    assert len(inputs) == 2
    assert all(max(shape) <= QR_MAX_SIDE for shape in inputs)


def test_qr_crop_fallback_is_bounded_even_with_oversize_detected_polygon():
    gray = np.zeros((3000, 3000), np.uint8)
    points = np.array([[[0, 0], [2499, 0], [2499, 2499], [0, 2499]]], np.float32)
    with patch('omr.reader.cv2.QRCodeDetector') as detector:
        detector.return_value.detectAndDecode.side_effect = [
            ('', points, None), ('EXAM|12345|hash', None, None),
        ]
        assert _read_qr(gray) == 'EXAM|12345|hash'
        calls = detector.return_value.detectAndDecode.call_args_list
        assert len(calls) == 2
        assert all(max(call.args[0].shape) <= QR_MAX_SIDE for call in calls)


@pytest.mark.parametrize('rotation', [0, 1, 2, 3])
def test_dense_qr_without_thumbnail_points_uses_bounded_tiles(rotation):
    """If downsampling loses QR detection entirely, tile original pixels safely."""
    data = ('abcdefghijklmnopqrstuvwxyz0123456789' * 6) + '|12345|74a38f7c85'
    gray = np.full((2480, 3508), 255, np.uint8)
    with make_qr_image(data) as qr:
        gray[100:313, 3150:3363] = np.asarray(
            qr.resize((213, 213), resample=Image.Resampling.NEAREST))
    gray = np.ascontiguousarray(np.rot90(gray, rotation))
    detector = cv2.QRCodeDetector()
    assert detector.detectAndDecode(gray)[0] == data
    inputs = []

    def decode(image):
        inputs.append(image.shape)
        # Exercise this path even if another OpenCV version finds the thumbnail.
        if len(inputs) == 1:
            return '', None, None
        return detector.detectAndDecode(image)

    with patch('omr.reader.cv2.QRCodeDetector') as factory:
        factory.return_value.detectAndDecode.side_effect = decode
        assert _read_qr(gray) == data
    assert len(inputs) >= 2
    assert all(max(shape) <= QR_MAX_SIDE for shape in inputs)
