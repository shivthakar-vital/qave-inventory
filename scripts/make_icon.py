"""Draw the app icon and write assets/icon.png + assets/icon.icns (macOS)."""
import os, shutil, subprocess, sys
from PyQt5.QtCore import Qt, QRectF
from PyQt5.QtGui import QGuiApplication, QImage, QPainter, QColor, QPen, QPainterPath

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")

def draw(size):
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 1024
    bg = QPainterPath()
    bg.addRoundedRect(QRectF(100 * s, 100 * s, 824 * s, 824 * s), 185 * s, 185 * s)
    p.fillPath(bg, QColor("#1D9E75"))
    # a stack of three boxes
    pen = QPen(QColor("#FFFFFF"), 36 * s)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(QColor("#E1F5EE"))
    for x, y in ((262, 520), (522, 520), (392, 290)):
        p.drawRoundedRect(QRectF(x * s, y * s, 240 * s, 220 * s), 22 * s, 22 * s)
        p.drawLine(int((x + 90) * s), int(y * s), int((x + 90) * s), int((y + 70) * s))
        p.drawLine(int((x + 150) * s), int(y * s), int((x + 150) * s), int((y + 70) * s))
    p.end()
    return img

if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    draw(1024).save(os.path.join(ROOT, "icon.png"))
    if sys.platform == "darwin":
        iconset = os.path.join(ROOT, "icon.iconset")
        os.makedirs(iconset, exist_ok=True)
        for n in (16, 32, 128, 256, 512):
            draw(n).save(os.path.join(iconset, f"icon_{n}x{n}.png"))
            draw(n * 2).save(os.path.join(iconset, f"icon_{n}x{n}@2x.png"))
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(ROOT, "icon.icns")], check=True)
        shutil.rmtree(iconset)
    print("wrote icon to", os.path.abspath(ROOT))
