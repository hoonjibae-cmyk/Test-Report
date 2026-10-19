import json
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
from omr.layout import SheetConfig,build_layout,layout_fingerprint
from omr.generator import render_sheet_image
from omr.reader import read_omr
from omr.simulate import simulate_marked,_apply_scan_distortion

def read_mark(schedule=None,style='exam',extra=False):
    cfg=SheetConfig(exam_id='placement',num_questions=30,id_digits=5,style=style,preferred_schedule=True)
    layout=build_layout(cfg)
    image=simulate_marked(cfg,{q:1+(q%5) for q in range(1,31)},'01234',dpi=180,distort=False,preferred_schedule=schedule)
    if extra:
        b=next(b for b in layout.bubbles if b.role=='schedule' and b.value==1)
        cv2.circle(image,(round(b.x_mm/25.4*180),round(b.y_mm/25.4*180)),round(layout.bubble_radius_mm/25.4*180*.8),(35,35,35),-1)
    image=_apply_scan_distortion(image,7)
    with tempfile.TemporaryDirectory() as d:
        p=Path(d);(p/'template.json').write_text(json.dumps(layout.template_dict(180)),encoding='utf-8');cv2.imwrite(str(p/'scan.png'),image)
        return read_omr(str(p/'scan.png'),str(p/'template.json'))

def test_preferences_survive_scan_without_changing_student_or_answers():
    for schedule in ['화목','월수금','미정','상관없음']:
        result=read_mark(schedule)
        assert result.preferred_schedule==schedule
        assert result.student_id_bubbles=='01234'
        assert result.answers()=={q:1+(q%5) for q in range(1,31)}

def test_blank_and_multiple_preferences_require_confirmation():
    for result in [read_mark(),read_mark('화목',extra=True)]:
        assert result.preferred_schedule is None
        assert any(f.get('type')=='preferred_schedule' for f in result.review_flags)

def test_existing_sheets_keep_their_coordinates_and_fingerprint():
    old=SheetConfig(num_questions=30,id_digits=5,style='exam')
    explicit=SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=False)
    new=SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=True)
    assert layout_fingerprint(old)==layout_fingerprint(explicit)
    assert layout_fingerprint(old)!=layout_fingerprint(new)
    assert [(b.role,b.index,b.value,b.x_mm,b.y_mm) for b in build_layout(old).bubbles]==[(b.role,b.index,b.value,b.x_mm,b.y_mm) for b in build_layout(new).bubbles if b.role!='schedule']
    for style in ['exam','basic']:
        render_sheet_image(build_layout(SheetConfig(style=style,preferred_schedule=True,id_digits=5)),dpi=100)

if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):
            fn();print('PASS',name)
