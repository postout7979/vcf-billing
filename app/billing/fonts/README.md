# 폰트 (한글 결산서 PDF용)

`NanumGothic.ttf` / `NanumGothicBold.ttf` — 네이버에서 배포하는 나눔고딕 폰트,
[SIL Open Font License 1.1](https://scripts.sil.org/OFL) 로 재배포 가능합니다.

`app/billing/statement_pdf.py`에서 reportlab으로 월간 결산서 PDF를 생성할 때
한글이 배포 환경(운영체제)에 관계없이 항상 동일하게 렌더링되도록 이 폰트를
직접 폰트 파일로 임베드합니다 (PDF 안에는 실제 사용된 글자만 서브셋으로
포함되어 파일 크기가 커지지 않습니다). 시스템에 한글 폰트가 없는 서버에
배포해도 문제 없이 동작합니다.
