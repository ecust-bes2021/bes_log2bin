import ctypes
import sys
import os
import time
import queue
import threading
import argparse
import yaml

#具备心跳检测的串口读取程序

# --- 配置 ---
# DLL 路径
# 确保 Python 架构 (32/64位) 与 DLL 架构匹配
if getattr(sys,'frozen',False):
    current_dir = os.path.dirname(sys.executable)
else:
    current_dir = os.path.dirname(__file__)
DLL_PATH = os.path.join(current_dir, "SerialPortLib_x64.dll")
CONFIG_PATH = os.path.join(current_dir, "config.yaml")

# --- 全局变量 ---
# 使用线程安全的队列作为回调和写入线程之间的缓冲区
data_queue = queue.Queue(maxsize=4096) # 限制队列大小，防止内存无限增长
# 停止写入线程的信号 (使用 None 作为哨兵)
stop_writer_signal = None

# 用于保存回调函数对象的引用，防止被垃圾回收
c_data_callback_instance = None
c_error_callback_instance = None

# 线程状态监控
class WriterThreadStatus:
    def __init__(self):
        self.running = False        # 线程是否运行中
        self.error = None           # 错误信息，如果有
        self.bytes_written = 0      # 已写入的字节数
        self.last_heartbeat = 0     # 最后一次心跳时间
        self.lock = threading.Lock() # 用于线程安全的访问状态

    def update(self, running=None, error=None, bytes_written=None):
        with self.lock:
            if running is not None:
                self.running = running
            if error is not None:
                self.error = error
            if bytes_written is not None:
                self.bytes_written = bytes_written
            self.last_heartbeat = time.time()

    def get_status(self):
        with self.lock:
            return {
                'running': self.running,
                'error': self.error,
                'bytes_written': self.bytes_written,
                'last_heartbeat': self.last_heartbeat
            }

    def is_alive(self, timeout_seconds=5):
        """ 检查线程是否存活，基于心跳超时 """
        with self.lock:
            return self.running and (time.time() - self.last_heartbeat) < timeout_seconds

# 创建全局线程状态对象
writer_status = WriterThreadStatus()

# --- ctypes 定义 ---

# 定义回调函数类型 (必须与 C 头文件中的定义匹配)
# typedef void (*SerialDataCallback)(void* user_data, const char* buffer, size_t length);
DATA_CALLBACK_FUNC = ctypes.CFUNCTYPE(
    None,                    # 返回类型: void (None in ctypes)
    ctypes.c_void_p,         # 参数1: void* user_data
    ctypes.POINTER(ctypes.c_char), # 参数2: const char* buffer (用 POINTER(c_char) 更精确)
    ctypes.c_size_t          # 参数3: size_t length
)

# typedef void (*SerialErrorCallback)(void* user_data, int error_code, const char* error_message);
ERROR_CALLBACK_FUNC = ctypes.CFUNCTYPE(
    None,                    # 返回类型: void
    ctypes.c_void_p,         # 参数1: void* user_data
    ctypes.c_int,            # 参数2: int error_code
    ctypes.c_char_p          # 参数3: const char* error_message
)

# --- Python 回调函数实现 ---

def py_data_callback(user_data_ptr, buffer_ptr, length):
    """
    C DLL 调用的数据接收回调函数。
    将数据放入队列，尽可能快地返回。
    """
    if buffer_ptr and length > 0:
        try:
            # 从 C 指针和长度创建 Python bytes 对象 (关键：这里会复制数据)
            # 使用切片比 string_at 更 Pythonic 一点，但效果类似
            # data_bytes = ctypes.string_at(buffer_ptr, length)
            # 或者使用更直接的指针访问（如果 buffer_ptr 类型正确）
            # 注意：要确保在数据被 C++ 覆盖前完成复制
            data_bytes = bytes(buffer_ptr[:length])

            # 将数据放入队列
            # 如果队列满了，put 会阻塞，或者使用 put_nowait/full() 进行处理
            try:
                data_queue.put_nowait(data_bytes)
            except queue.Full:
                print("警告：Python 数据队列已满，可能丢失数据！", file=sys.stderr)
                # 这里可以考虑其他策略，比如丢弃旧数据或扩展队列
        except Exception as e:
            print(f"错误：在数据回调中发生异常: {e}", file=sys.stderr)
            # 避免回调函数抛出未处理异常导致 C++ 层崩溃

def py_error_callback(user_data_ptr, error_code, error_message_ptr):
    """C DLL 调用的错误回调函数。"""
    try:
        error_message = error_message_ptr.decode('utf-8', errors='replace') if error_message_ptr else "N/A"
        print(f"[DLL ERROR] Code: {error_code}, Message: {error_message}", file=sys.stderr)
        # 这里可以根据错误代码执行特定操作，比如设置停止标志
        # if error_code == SOME_FATAL_ERROR_CODE:
        #    global g_stop_event (需要定义一个全局事件)
        #    g_stop_event.set()
    except Exception as e:
         print(f"错误：在错误回调中发生异常: {e}", file=sys.stderr)


# --- 文件写入线程 ---
def writer_thread_func(filename):
    """从队列读取数据并写入二进制文件。"""
    print(f"写入线程启动，将数据写入 '{filename}'")
    written_bytes_total = 0
    heartbeat_interval = 1.0  # 心跳间隔，秒
    last_heartbeat = time.time()
    last_flush = time.time()
    flush_interval = 5.0  # 定期刷新文件，秒
    max_retry_count = 3  # 最大重试次数

    # 更新线程状态为运行中
    writer_status.update(running=True, bytes_written=0)

    try:
        # 尝试打开文件，带重试
        file_handle = None
        retry_count = 0

        while retry_count < max_retry_count and file_handle is None:
            try:
                file_handle = open(filename, 'wb')
            except IOError as e:
                retry_count += 1
                error_msg = f"错误：打开输出文件失败 (尝试 {retry_count}/{max_retry_count}): {e}"
                print(error_msg, file=sys.stderr)
                writer_status.update(error=error_msg)

                if retry_count >= max_retry_count:
                    raise IOError(f"无法打开输出文件，已达到最大重试次数: {e}")

                # 等待一段时间再重试
                time.sleep(1.0)

        with file_handle:
            while True:
                # 心跳检测
                current_time = time.time()
                if current_time - last_heartbeat >= heartbeat_interval:
                    writer_status.update(bytes_written=written_bytes_total)
                    last_heartbeat = current_time

                # 定期刷新文件
                if current_time - last_flush >= flush_interval:
                    try:
                        file_handle.flush()
                        last_flush = current_time
                    except IOError as e:
                        error_msg = f"警告：文件刷新失败: {e}"
                        print(error_msg, file=sys.stderr)
                        writer_status.update(error=error_msg)

                try:
                    # 从队列获取数据，设置超时以允许检查退出信号
                    data_chunk = data_queue.get(timeout=0.5) # 等待最多0.5秒

                    if data_chunk is stop_writer_signal: # 检查停止信号
                        print("写入线程收到停止信号，正在退出...")
                        break # 退出循环

                    if data_chunk:
                        try:
                            file_handle.write(data_chunk)
                            written_bytes_total += len(data_chunk)
                            # 每写入 1MB 更新一次状态
                            if written_bytes_total % (1024 * 1024) == 0:
                                writer_status.update(bytes_written=written_bytes_total)
                                print(f"已写入 {written_bytes_total / (1024*1024):.2f} MB")
                        except IOError as e:
                            error_msg = f"错误：文件写入失败: {e}"
                            print(error_msg, file=sys.stderr)
                            writer_status.update(error=error_msg)
                            # 尝试重新打开文件
                            raise

                except queue.Empty:
                    # 队列为空，继续循环等待
                    continue
                except IOError as e:
                    # 文件IO错误，尝试重新打开文件
                    error_msg = f"错误：文件写入失败: {e}"
                    print(error_msg, file=sys.stderr)
                    writer_status.update(error=error_msg)

                    # 尝试重新打开文件
                    try:
                        file_handle.close()
                        file_handle = open(filename, 'ab')  # 以追加模式打开
                        print(f"已重新打开文件 '{filename}' 继续写入")
                    except IOError as reopen_error:
                        error_msg = f"错误：无法重新打开文件: {reopen_error}"
                        print(error_msg, file=sys.stderr)
                        writer_status.update(error=error_msg)
                        break  # 无法恢复，退出线程
                except Exception as e:
                    error_msg = f"错误：写入线程发生未知异常: {e}"
                    print(error_msg, file=sys.stderr)
                    writer_status.update(error=error_msg)
                    # 对于未知异常，我们选择退出线程
                    break
    except Exception as e:
        error_msg = f"错误：写入线程初始化失败: {e}"
        print(error_msg, file=sys.stderr)
        writer_status.update(running=False, error=error_msg)
    finally:
        # 确保更新线程状态
        writer_status.update(running=False, bytes_written=written_bytes_total)
        print(f"写入线程结束。总共写入 {written_bytes_total} 字节。")

# --- 读取配置文件 ---
def read_config():
    """读取配置文件，获取波特率、COM端口和输出文件路径"""
    if not os.path.exists(CONFIG_PATH):
        print(f"错误：找不到配置文件: {CONFIG_PATH}", file=sys.stderr)
        print(f"请创建配置文件，格式如下:", file=sys.stderr)
        print(f"# config.yaml 示例", file=sys.stderr)
        print(f"com_port: COM3", file=sys.stderr)
        print(f"baud_rate: 12000000", file=sys.stderr)
        print(f"output_dir: D:\\workdir\\ACE-8.0.2\\bes_log2bin\\logs", file=sys.stderr)
        print(f"suffix: test", file=sys.stderr)
        print(f"", file=sys.stderr)
        print(f"注意: 在YAML中，Windows路径可以直接使用单反斜杠，无需转义", file=sys.stderr)
        sys.exit(1)
        
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
            
        # 检查必要的配置项
        if 'com_port' not in config:
            print(f"错误：配置文件缺少 'com_port' 项", file=sys.stderr)
            sys.exit(1)
            
        if 'baud_rate' not in config:
            print(f"错误：配置文件缺少 'baud_rate' 项", file=sys.stderr)
            sys.exit(1)
            
        if 'output_dir' not in config:
            print(f"错误：配置文件缺少 'output_dir' 项", file=sys.stderr)
            sys.exit(1)
            
        # 提取COM端口
        com_port = config['com_port']
        if not com_port.startswith("COM"):
            print(f"错误：串口格式不正确，应为 COMx，得到的是 '{com_port}'", file=sys.stderr)
            sys.exit(1)
            
        # 检查波特率是否为整数
        try:
            baud_rate = int(config['baud_rate'])
        except ValueError:
            print(f"错误：波特率必须是数字，得到的是 '{config['baud_rate']}'", file=sys.stderr)
            sys.exit(1)
            
        # 标准化路径 - 确保使用系统正确的分隔符
        output_dir = os.path.normpath(config['output_dir'])
        
        # 获取后缀名（可选）
        suffix = config.get('suffix', '')
        
        # 确保输出目录存在
        try:
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
        except Exception as e:
            print(f"错误：创建输出目录失败: {e}", file=sys.stderr)
            sys.exit(1)
            
        # 生成基于时间戳的文件名
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")
        # 如果有后缀名，则添加到文件名中
        if suffix:
            output_file = os.path.join(output_dir, f"{timestamp}-{suffix}.bin")
        else:
            output_file = os.path.join(output_dir, f"{timestamp}.bin")
            
        return f"\\\\.\\{com_port}", baud_rate, output_file
        
    except yaml.YAMLError as e:
        print(f"错误：配置文件格式不正确: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"错误：读取配置文件失败: {e}", file=sys.stderr)
        sys.exit(1)

# --- 命令行参数解析 ---
def parse_arguments():
    parser = argparse.ArgumentParser(description='Log to Binary Converter')
    args = parser.parse_args()

    try:
        # 读取配置文件
        serial_port, baud_rate, output_file = read_config()
        return serial_port, baud_rate, output_file
    except Exception as e:
        print(f"错误：解析参数失败: {e}", file=sys.stderr)
        print(r"正确格式: xxx.exe", file=sys.stderr)
        sys.exit(1)

# --- 主程序 ---
if __name__ == "__main__":
    # 解析命令行参数
    SERIAL_PORT, BAUD_RATE, OUTPUT_FILENAME = parse_arguments()
    print(f"配置信息：")
    print(f"  串口: {SERIAL_PORT}")
    print(f"  波特率: {BAUD_RATE} bps")
    print(f"  输出文件: {OUTPUT_FILENAME}")

    # 1. 检查并加载 DLL
    if not os.path.exists(DLL_PATH):
        print(f"错误：找不到 DLL 文件: {DLL_PATH}")
        sys.exit(1)

    try:
        # 如果使用了默认的 __cdecl (extern "C" 在 MSVC 下通常是 cdecl)，用 CDLL
        # 根据你 DLL 的编译方式选择，CDLL 更常见于非 Windows API 的自定义库
        ser_lib = ctypes.CDLL(DLL_PATH)
        print(f"成功加载 DLL: {DLL_PATH}")
    except OSError as e:
        print(f"错误：加载 DLL 失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. 定义导出函数的参数类型 (argtypes) 和返回类型 (restype)
    try:
        # SerialPort_Open
        ser_lib.SerialPort_Open.argtypes = [
            ctypes.c_char_p,          # portName
            ctypes.c_ulong,           # baudRate
            DATA_CALLBACK_FUNC,       # dataCallback
            ERROR_CALLBACK_FUNC,      # errorCallback
            ctypes.c_void_p           # userData (可以传 None)
        ]
        ser_lib.SerialPort_Open.restype = ctypes.c_int

        # SerialPort_Close
        ser_lib.SerialPort_Close.argtypes = []
        ser_lib.SerialPort_Close.restype = ctypes.c_int

        # SerialPort_Write (如果需要发送)
        ser_lib.SerialPort_Write.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        ser_lib.SerialPort_Write.restype = ctypes.c_int

        # SerialPort_IsOpen
        ser_lib.SerialPort_IsOpen.argtypes = []
        ser_lib.SerialPort_IsOpen.restype = ctypes.c_int

    except AttributeError as e:
        print(f"错误：DLL 中缺少必要的导出函数: {e}", file=sys.stderr)
        sys.exit(1)

    # 3. 创建回调函数实例并保持引用！
    c_data_callback_instance = DATA_CALLBACK_FUNC(py_data_callback)
    c_error_callback_instance = ERROR_CALLBACK_FUNC(py_error_callback)

    # 4. 启动写入线程
    writer_thread = threading.Thread(target=writer_thread_func, args=(OUTPUT_FILENAME,), daemon=True)
    # daemon=True 意味着如果主线程退出，写入线程也会被强制终止（可能丢失缓冲区数据）
    # 如果希望写入线程完成所有写入，不要设为 daemon，并确保主线程会 join() 它
    writer_thread.start()

    # 5. 打开串口
    print(f"正在打开串口 {SERIAL_PORT} @ {BAUD_RATE} bps...")
    # 将 Python 字符串编码为字节串传递给 c_char_p
    port_name_bytes = SERIAL_PORT.encode('ascii') # 或者 utf-8，取决于 DLL 期望
    result = ser_lib.SerialPort_Open(
        port_name_bytes,
        BAUD_RATE,
        c_data_callback_instance,
        c_error_callback_instance,
        None # user_data, 可以传递需要回传给回调的 Python 对象指针 (需额外处理)
    )

    if result != 0:
        print(f"打开串口失败，错误码: {result}")
        # 尝试通知写入线程停止 (虽然它可能还没写任何东西)
        data_queue.put(stop_writer_signal)
        writer_thread.join(timeout=2) # 等待写入线程退出
        sys.exit(1)

    print("串口打开成功。正在接收数据...\n")
    print(f"数据将保存到 '{OUTPUT_FILENAME}'。按 Ctrl+C 停止。")

    # 6. 保持主线程运行，直到用户中断或发生错误
    try:
        check_interval = 5.0  # 检查间隔，秒
        heartbeat_timeout = 10.0  # 心跳超时，秒
        last_status_check = time.time()
        status_check_interval = 30.0  # 状态检查间隔，秒

        while ser_lib.SerialPort_IsOpen():
            current_time = time.time()

            # 检查写入线程状态
            if not writer_thread.is_alive():
                # 如果线程对象显示线程已终止，检查状态对象
                status = writer_status.get_status()
                if status['running']:
                    # 状态不一致，可能是线程崩溃
                    error_msg = f"错误：写入线程已终止，但状态显示仍在运行。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)
                    # 更新状态以反映线程已终止
                    writer_status.update(running=False, error=error_msg)
                    break
                elif data_queue.empty():
                    # 如果队列为空且线程已终止，可能是正常终止
                    error_msg = f"错误：写入线程已终止。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)
                    break
                else:
                    # 队列不为空但线程已终止，尝试重启写入线程
                    print("警告：写入线程已终止，但队列中还有数据。尝试重启写入线程...")
                    # 创建新的写入线程
                    writer_thread = threading.Thread(target=writer_thread_func, args=(OUTPUT_FILENAME,), daemon=True)
                    writer_thread.start()

            # 定期检查写入线程心跳
            if current_time - last_status_check >= status_check_interval:
                if not writer_status.is_alive(heartbeat_timeout):
                    status = writer_status.get_status()
                    error_msg = f"错误：写入线程心跳超时。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)

                    # 尝试重启写入线程
                    if writer_thread.is_alive():
                        print("尝试终止当前写入线程...")
                        data_queue.put(stop_writer_signal)  # 发送停止信号
                        writer_thread.join(timeout=2)  # 等待线程终止

                    print("重启写入线程...")
                    writer_thread = threading.Thread(target=writer_thread_func, args=(OUTPUT_FILENAME,), daemon=True)
                    writer_thread.start()

                # 显示当前状态
                status = writer_status.get_status()
                bytes_written = status['bytes_written']
                print(f"状态检查: 已写入 {bytes_written / (1024*1024):.2f} MB, 线程运行中: {status['running']}")
                last_status_check = current_time

            # 休眠一段时间
            time.sleep(check_interval)

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止...")
    except Exception as e:
        print(f"\n主循环发生异常: {e}", file=sys.stderr)
    finally:
        # 7. 关闭串口并清理
        print("正在关闭串口...")
        if ser_lib.SerialPort_IsOpen():
            close_result = ser_lib.SerialPort_Close()
            if close_result != 0:
                print(f"关闭串口时发生错误，错误码: {close_result}", file=sys.stderr)
            else:
                print("串口已关闭。")

        # 8. 通知写入线程停止并等待其完成
        print("正在停止写入线程...")

        # 检查写入线程状态
        if writer_thread.is_alive():
            try:
                # 尝试正常停止
                data_queue.put(stop_writer_signal) # 发送停止信号
                writer_thread.join(timeout=5) # 等待写入线程完成，设置超时

                # 如果线程仍然在运行，检查状态
                if writer_thread.is_alive():
                    status = writer_status.get_status()
                    print(f"警告：写入线程在超时后仍未结束。状态: {status}", file=sys.stderr)

                    # 尝试再次发送停止信号
                    print("再次尝试停止写入线程...")
                    data_queue.put(stop_writer_signal) # 再次发送停止信号
                    writer_thread.join(timeout=2) # 等待短时间

                    if writer_thread.is_alive():
                        print("警告：无法正常停止写入线程，程序将继续退出。", file=sys.stderr)
                        # 在这里我们不使用 thread._stop() 或类似方法强制终止线程
                        # 因为这些方法在 Python 3 中已经被弃用并且不安全
            except Exception as e:
                print(f"关闭写入线程时发生异常: {e}", file=sys.stderr)
        else:
            # 如果线程已经不在运行，检查其状态
            status = writer_status.get_status()
            if status['running']:
                print(f"警告：写入线程已终止，但状态显示仍在运行。最后错误: {status['error']}", file=sys.stderr)
                writer_status.update(running=False)
            else:
                print(f"写入线程已经结束。最后状态: 已写入 {status['bytes_written']} 字节")

        print("程序结束。")