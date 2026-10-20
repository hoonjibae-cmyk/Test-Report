import json
import tempfile
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cv2
from omr.layout import ENROLLMENT_LABELS,SheetConfig,build_layout,layout_fingerprint
from omr.generator import render_sheet_image
from omr.reader import read_omr
from omr.simulate import simulate_marked,_apply_scan_distortion

def read_mark(schedule=None,style='exam',extra=False,faint=None):
    cfg=SheetConfig(exam_id='placement',num_questions=30,id_digits=5,style=style,preferred_schedule=True)
    layout=build_layout(cfg)
    image=simulate_marked(cfg,{q:1+(q%5) for q in range(1,31)},'01234',dpi=180,distort=False,preferred_schedule=schedule)
    if extra:
        b=next(b for b in layout.bubbles if b.role=='schedule' and b.value==1)
        cv2.circle(image,(round(b.x_mm/25.4*180),round(b.y_mm/25.4*180)),round(layout.bubble_radius_mm/25.4*180*.8),(35,35,35),-1)
    if faint is not None:
        # 흐린 표기 — 버블 반지름의 절반만 칠한다(채움률이 '확실한 표기' 선에 못 미친다)
        b=next(b for b in layout.bubbles if b.role=='schedule' and b.value==faint)
        cv2.circle(image,(round(b.x_mm/25.4*180),round(b.y_mm/25.4*180)),round(layout.bubble_radius_mm/25.4*180*.45),(35,35,35),-1)
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

def schedule_flag(result):
    return next(f for f in result.review_flags if f.get('type')=='preferred_schedule')

def test_unreadable_preferences_still_name_the_darkest_bubble():
    # 값으로 삼지 못해도 '어느 칸이 가장 진했는지'는 넘긴다 — 웹이 제안값으로 띄운다.
    faint=read_mark(faint=2)
    assert faint.preferred_schedule is None
    assert schedule_flag(faint)['candidate']=='미정'
    assert schedule_flag(faint)['certain'] is False

    doubled=read_mark('화목',extra=True)
    assert schedule_flag(doubled)['status']=='multiple'
    assert sorted(schedule_flag(doubled)['selected'])==['월수금','화목']
    assert schedule_flag(doubled)['candidate'] in ('화목','월수금')

    # 아무것도 안 칠한 답안지는 제안할 것이 없고, 비었다는 것만은 확실하다
    blank=read_mark()
    assert schedule_flag(blank)['status']=='blank'
    assert schedule_flag(blank)['certain'] is True
    assert schedule_flag(blank)['candidate'] is None

def test_existing_sheets_keep_their_coordinates_and_fingerprint():
    old=SheetConfig(num_questions=30,id_digits=5,style='exam')
    explicit=SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=False)
    new=SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=True)
    assert layout_fingerprint(old)==layout_fingerprint(explicit)
    assert layout_fingerprint(old)!=layout_fingerprint(new)
    assert [(b.role,b.index,b.value,b.x_mm,b.y_mm) for b in build_layout(old).bubbles]==[(b.role,b.index,b.value,b.x_mm,b.y_mm) for b in build_layout(new).bubbles if b.role!='schedule']
    for style in ['exam','basic']:
        render_sheet_image(build_layout(SheetConfig(style=style,preferred_schedule=True,id_digits=5)),dpi=100)

def read_placement(schedule='화목',enrollment='신규생',student='1234'):
    """재원 구분 칸까지 있는 반배치고사 답안지(지금 배치)를 칠해 읽는다."""
    cfg=SheetConfig(exam_id='placement',num_questions=30,id_digits=5,style='exam',preferred_schedule=True,enrollment_mark=True)
    layout=build_layout(cfg)
    image=simulate_marked(cfg,{q:1+(q%5) for q in range(1,31)},student,dpi=180,distort=False,preferred_schedule=schedule,enrollment=enrollment)
    image=_apply_scan_distortion(image,7)
    with tempfile.TemporaryDirectory() as d:
        p=Path(d);(p/'template.json').write_text(json.dumps(layout.template_dict(180)),encoding='utf-8');cv2.imwrite(str(p/'scan.png'),image)
        return read_omr(str(p/'scan.png'),str(p/'template.json'))

def test_enrollment_mark_reads_alongside_everything_else():
    for enrollment in ENROLLMENT_LABELS:
        result=read_placement(enrollment=enrollment)
        assert result.enrollment==enrollment
        assert result.preferred_schedule=='화목'
        # 신규생은 수험번호 칸에 전화번호 뒤 4자리를 쓴다 — 4자리가 그대로 읽혀야 한다
        assert result.student_id_bubbles=='1234'
        assert result.answers()=={q:1+(q%5) for q in range(1,31)}
        assert not [f for f in result.review_flags if f.get('type') in ('enrollment','preferred_schedule')]

def test_blank_enrollment_mark_leaves_a_flag_not_a_guess():
    result=read_placement(enrollment=None)
    assert result.enrollment is None
    flag=next(f for f in result.review_flags if f.get('type')=='enrollment')
    assert flag['status']=='blank' and flag['certain'] is True and flag['candidate'] is None
    # 옆 칸의 판정은 그대로다 — 칸 하나가 비었다고 선호 요일까지 흔들리지 않는다
    assert result.preferred_schedule=='화목'

def test_enrollment_layout_moves_id_grid_and_schedule_down():
    legacy=build_layout(SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=True))
    new=build_layout(SheetConfig(num_questions=30,id_digits=5,style='exam',preferred_schedule=True,enrollment_mark=True))
    assert layout_fingerprint(legacy.config)!=layout_fingerprint(new.config)
    assert not [b for b in legacy.bubbles if b.role=='enrollment']
    assert len([b for b in new.bubbles if b.role=='enrollment'])==2
    # 재원 구분 칸이 끼는 만큼 수험번호 그리드는 12mm, 선호 요일은 11mm 내려간다
    ids=lambda l:[(b.index,b.value,b.x_mm,b.y_mm) for b in l.bubbles if b.role=='id']
    assert [(i,v,x,y+12) for i,v,x,y in ids(legacy)]==ids(new)
    sch=lambda l:[(b.value,b.x_mm,b.y_mm) for b in l.bubbles if b.role=='schedule']
    assert [(v,x,y+11) for v,x,y in sch(legacy)]==sch(new)
    # 문항 버블은 그대로다
    qs=lambda l:[(b.index,b.value,b.x_mm,b.y_mm) for b in l.bubbles if b.role=='question']
    assert qs(legacy)==qs(new)
    # 예전 배치(재원 구분 칸 없음)는 손대지 않는다 — 이미 뽑아 둔 종이가 있을 수 있다
    assert ids(legacy)[0][3]==legacy.ref_top_mm+91
    for style in ['exam','basic']:
        render_sheet_image(build_layout(SheetConfig(style=style,preferred_schedule=True,enrollment_mark=True,id_digits=5)),dpi=100)
    # 서술형과 함께여도 그려진다(안내문이 다섯 줄이 된다)
    render_sheet_image(build_layout(SheetConfig(style='exam',preferred_schedule=True,enrollment_mark=True,id_digits=5,essay_count=2)),dpi=100)

def test_sheets_without_placement_fields_are_untouched():
    plain=SheetConfig(num_questions=45,id_digits=5,style='exam')
    assert layout_fingerprint(plain)==layout_fingerprint(SheetConfig(num_questions=45,id_digits=5,style='exam',enrollment_mark=False))
    assert not [b for b in build_layout(plain).bubbles if b.role in ('schedule','enrollment')]

if __name__=='__main__':
    for name,fn in list(globals().items()):
        if name.startswith('test_'):
            fn();print('PASS',name)
