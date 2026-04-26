import sys
import os
import psutil
import time
import gc
try:
    import winreg
except ImportError:
    winreg = None

from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction, QInputDialog, QMessageBox
from PyQt5.QtGui import QIcon, QMovie, QPainter, QColor, QPixmap, qAlpha
from PyQt5.QtCore import QTimer, Qt, QRect

def get_resource_path(relative_path):
    """ Get absolute path to resource for dev and PyInstaller """
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, *relative_path.replace('\\', '/').split('/'))

class MemoryManager:
    def __init__(self):
        self.chomped_memory = []
        self.safety_reserve_gb = 1.5
        self.safety_reserve_percent = 0.10
        self.lifetime_bytes = 0
        self.integral_bytes_seconds = 0.0
        self.start_time = time.time()
        self.last_update_time = self.start_time

    def update_integral(self):
        now = time.time()
        duration = now - self.last_update_time
        current_bytes = self.get_current_chomped_bytes()
        self.integral_bytes_seconds += current_bytes * duration
        self.last_update_time = now

    def get_available_memory_bytes(self):
        return psutil.virtual_memory().available

    def get_total_memory_bytes(self):
        return psutil.virtual_memory().total

    def get_safety_threshold_bytes(self):
        total = self.get_total_memory_bytes()
        reserve_by_gb = self.safety_reserve_gb * 1024**3
        reserve_by_percent = total * self.safety_reserve_percent
        return int(max(reserve_by_gb, reserve_by_percent))

    def can_chomp(self, amount_mb):
        amount_bytes = amount_mb * 1024 * 1024
        available = self.get_available_memory_bytes()
        threshold = self.get_safety_threshold_bytes()
        
        if (available - amount_bytes) < threshold:
            return False, f"Safety threshold reached! Must keep at least {threshold / 1024**3:.1f}GB free."
        return True, ""

    def chomp(self, amount_mb):
        self.update_integral()
        success, message = self.can_chomp(amount_mb)
        if not success:
            return False, message
        
        try:
            chunk_size_mb = 256
            remaining_mb = amount_mb
            
            while remaining_mb > 0:
                current_chunk = min(remaining_mb, chunk_size_mb)
                size_bytes = int(current_chunk * 1024 * 1024)
                arr = bytearray(size_bytes)
                if len(arr) > 0:
                    arr[0] = 1
                    arr[-1] = 1
                self.chomped_memory.append(arr)
                self.lifetime_bytes += size_bytes
                remaining_mb -= current_chunk
                
            return True, f"Chomped {amount_mb}MB"
        except MemoryError:
            return False, "System refused to allocate more memory."
        except Exception as e:
            return False, str(e)

    def release(self):
        self.update_integral()
        self.chomped_memory.clear()
        gc.collect()

    def get_current_chomped_bytes(self):
        return sum(len(arr) for arr in self.chomped_memory)

    def get_current_chomped_gb(self):
        return self.get_current_chomped_bytes() / 1024**3

    def get_stats(self):
        self.update_integral()
        uptime = max(0.1, time.time() - self.start_time)
        lifetime_gb = self.lifetime_bytes / 1024**3
        average_load_gb = (self.integral_bytes_seconds / uptime) / 1024**3
        return lifetime_gb, average_load_gb

    def check_and_fart(self):
        """ Automatically release memory if system needs it """
        available = self.get_available_memory_bytes()
        threshold = self.get_safety_threshold_bytes()
        
        farted_count = 0
        # Release chunks one by one if we're under the threshold
        while (available < threshold) and self.chomped_memory:
            # Pop the oldest chunk (FIFO)
            self.chomped_memory.pop(0)
            farted_count += 1
            # Recalculate available (approximate or refresh)
            available = self.get_available_memory_bytes()
            
        if farted_count > 0:
            gc.collect()
            return True, farted_count
        return False, 0

class RAMChomperTray(QSystemTrayIcon):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.mem_manager = MemoryManager()
        
        # Initial visible icon
        self.setIcon(QIcon(get_resource_path("icon.ico")))
        
        # Load Animation
        gif_path = get_resource_path("Public/animation.gif.gif")
        if not os.path.exists(gif_path):
            gif_path = get_resource_path("public/animation.gif.gif")
        
        self.movie = QMovie(gif_path)
        self.movie.setCacheMode(QMovie.CacheAll)
        self.movie.start()
        
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_icon)
        self.timer.start(200)
        
        self.setup_menu()
        self.setToolTip("RAM Chomper - Idle")
        self.show()

    def setup_menu(self):
        menu = QMenu()
        
        self.status_action = QAction("Current: 0.00 GB", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)
        
        self.lifetime_action = QAction("Lifetime: 0.00 GB", menu)
        self.lifetime_action.setEnabled(False)
        menu.addAction(self.lifetime_action)
        
        self.rate_action = QAction("Average Load: 0.00 GB", menu)
        self.rate_action.setEnabled(False)
        menu.addAction(self.rate_action)
        
        menu.addSeparator()
        
        presets = [("Chomp 512 MB", 512), ("Chomp 1 GB", 1024), 
                   ("Chomp 2 GB", 2048), ("Chomp 4 GB", 4096)]
        
        for label, mb in presets:
            action = QAction(label, menu)
            action.triggered.connect(lambda checked, m=mb: self.chomp_memory(m))
            menu.addAction(action)
            
        custom_action = QAction("Custom Chomp...", menu)
        custom_action.triggered.connect(self.custom_chomp)
        menu.addAction(custom_action)
        
        menu.addSeparator()
        
        release_action = QAction("Stop Chomping", menu)
        release_action.triggered.connect(self.release_memory)
        menu.addAction(release_action)
        
        menu.addSeparator()
        
        disclaimer_action = QAction("Disclaimer", menu)
        disclaimer_action.triggered.connect(self.show_disclaimer)
        menu.addAction(disclaimer_action)
        
        exit_action = QAction("Exit", menu)
        exit_action.triggered.connect(self.app.quit)
        menu.addAction(exit_action)
        
        self.setContextMenu(menu)

    def is_dark_mode(self):
        if winreg:
            try:
                registry = winreg.ConnectRegistry(None, winreg.HKEY_CURRENT_USER)
                key = winreg.OpenKey(registry, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
                value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
                return value == 0
            except:
                return True
        return True

    def tint_pixmap(self, pixmap, color):
        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.transparent)
        painter = QPainter(tinted)
        painter.drawPixmap(0, 0, pixmap)
        painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
        painter.fillRect(tinted.rect(), color)
        painter.end()
        return tinted

    def update_icon(self):
        pixmap = self.movie.currentPixmap()
        if not pixmap.isNull() and pixmap.width() > 0:
            image = pixmap.toImage()
            content_rect = self.get_content_rect(image)
            if content_rect.isValid() and content_rect.width() > 0:
                pixmap = pixmap.copy(content_rect)

            pixmap = pixmap.scaled(64, 64, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            color = QColor(255, 255, 255) if self.is_dark_mode() else QColor(0, 0, 0)
            tinted_pixmap = self.tint_pixmap(pixmap, color)
            
            if not tinted_pixmap.isNull():
                self.setIcon(QIcon(tinted_pixmap))
        # Check for boundary cross (Farting)
        farted, count = self.mem_manager.check_and_fart()
        
        chomped_gb = self.mem_manager.get_current_chomped_gb()
        lifetime_gb, avg_rate = self.mem_manager.get_stats()
        
        new_interval = max(30, int(200 - (chomped_gb * 40)))
        if self.timer.interval() != new_interval:
            self.timer.setInterval(new_interval)
            
        self.status_action.setText(f"Current: {chomped_gb:.2f} GB")
        self.lifetime_action.setText(f"Lifetime: {lifetime_gb:.2f} GB")
        self.rate_action.setText(f"Average Load: {avg_rate:.2f} GB")
        
        if farted:
            self.setToolTip(f"RAM Chomper - FARTING (Released {count} chunks for system!)")
        elif chomped_gb > 0:
            self.setToolTip(f"RAM Chomper - Chomping {chomped_gb:.2f} GB")
        else:
            self.setToolTip("RAM Chomper - Idle")

    def get_content_rect(self, image):
        width, height = image.width(), image.height()
        left, top, right, bottom = width, height, 0, 0
        found = False
        for y in range(height):
            for x in range(width):
                if qAlpha(image.pixel(x, y)) > 10:
                    found = True
                    if x < left: left = x
                    if x > right: right = x
                    if y < top: top = y
                    if y > bottom: bottom = y
        return QRect(left, top, right - left + 1, bottom - top + 1) if found else image.rect()

    def chomp_memory(self, mb):
        success, message = self.mem_manager.chomp(mb)
        if not success:
            QMessageBox.warning(None, "Chomp Failed", message)

    def custom_chomp(self):
        available = self.mem_manager.get_available_memory_bytes()
        threshold = self.mem_manager.get_safety_threshold_bytes()
        max_mb = max(0, int((available - threshold) / 1024**2))
        
        if max_mb <= 0:
            QMessageBox.warning(None, "Safety Limit", "Cannot chomp more!")
            return

        val, ok = QInputDialog.getInt(None, "Custom Chomp", 
                                      f"Amount (MB):\nMax safe: {max_mb} MB", 
                                      1024, 0, max_mb, 128)
        if ok:
            self.chomp_memory(val)

    def show_disclaimer(self):
        QMessageBox.information(None, "Disclaimer", 
            "This is a stupid app made for fun. It literally just eats RAM for no reason.\n\n"
            "Use it at your own risk. While it has safety checks, intentionally filling your RAM "
            "is generally a silly thing to do. The author is not responsible for any "
            "crashes, lost data, or existential crises caused by your lack of available memory.")

    def release_memory(self):
        self.mem_manager.release()

def main():
    if hasattr(Qt, 'AA_EnableHighDpiScaling'):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, 'AA_UseHighDpiPixmaps'):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(QIcon(get_resource_path("icon.ico")))
    
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return 1
    
    tray = RAMChomperTray(app)
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
