DARK_QSS = """
QWidget {
    background-color: #0f1115;
    color: #e8eaf0;
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #0f1115;
}
#HeaderTitle {
    font-size: 18px;
    font-weight: 700;
    color: #ffffff;
}
#HeaderSub {
    font-size: 11px;
    color: #8a91a5;
}
#StatusDot {
    border-radius: 7px;
    background-color: #3a4152;
}
QFrame#SidePanel {
    background-color: #14171e;
    border: 1px solid #222733;
    border-radius: 14px;
}
QLabel#PanelTitle {
    font-size: 12px;
    font-weight: 700;
    color: #9aa3b8;
    letter-spacing: 1px;
}
QComboBox, QSpinBox {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 6px 10px;
    color: #e8eaf0;
    min-height: 20px;
}
QComboBox:hover, QSpinBox:hover {
    border-color: #4f8cff;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    selection-background-color: #2b3a5e;
    color: #e8eaf0;
    outline: none;
}
QPushButton {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 8px;
    padding: 7px 14px;
    color: #dfe3ee;
}
QPushButton:hover {
    background-color: #232936;
    border-color: #3d4656;
}
QPushButton:disabled {
    color: #5a6172;
    background-color: #171b23;
}
QPushButton#PrimaryButton {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4f8cff, stop:1 #7b5cff);
    border: none;
    color: #ffffff;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 18px;
    border-radius: 10px;
}
QPushButton#PrimaryButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #619aff, stop:1 #8d74ff);
}
QPushButton#StopButton {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ff5c7a, stop:1 #ff7b4f);
    border: none;
    color: #ffffff;
    font-weight: 700;
    font-size: 14px;
    padding: 10px 18px;
    border-radius: 10px;
}
QFrame#CaptionCard {
    background-color: #161a22;
    border: 1px solid #242a38;
    border-radius: 12px;
}
QLabel#CaptionSource {
    color: #8a91a5;
    font-size: 12px;
}
QLabel#CaptionTarget {
    color: #ffffff;
    font-size: 15px;
    font-weight: 600;
}
QLabel#CaptionMeta {
    color: #5a6172;
    font-size: 11px;
}
QLabel#EmptyHint {
    color: #5a6172;
    font-size: 14px;
    background: transparent;
}
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #2c3345;
    border-radius: 4px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #3a4360;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0px;
}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: transparent;
}
QStatusBar {
    background-color: #12151c;
    color: #8a91a5;
    border-top: 1px solid #1e2330;
}
QProgressBar {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 6px;
    text-align: center;
    color: #e8eaf0;
    height: 16px;
}
QProgressBar::chunk {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f8cff, stop:1 #7b5cff);
    border-radius: 5px;
}
QToolTip {
    background-color: #1b1f29;
    color: #e8eaf0;
    border: 1px solid #2a3040;
    padding: 4px;
}
#LevelBar {
    background-color: #1b1f29;
    border: 1px solid #2a3040;
    border-radius: 4px;
}
"""

OVERLAY_QSS = """
QWidget#OverlayRoot {
    background-color: rgba(12, 14, 20, 200);
    border-radius: 14px;
    border: 1px solid rgba(255, 255, 255, 24);
}
QLabel#OverlaySource {
    color: rgba(255, 255, 255, 150);
    font-size: 13px;
    background: transparent;
}
QLabel#OverlayTarget {
    color: #ffffff;
    font-size: 18px;
    font-weight: 700;
    background: transparent;
}
"""
