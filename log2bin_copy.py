# 导入必要的Python库
import ctypes  # 用于调用C语言编写的动态链接库(DLL)
import sys     # 提供与Python解释器交互的功能
import os      # 提供操作系统相关功能
import time    # 提供时间相关功能
import queue   # 提供线程安全的队列实现
import threading  # 提供多线程支持
import argparse  # 提供命令行参数解析功能

# 这是一个具备心跳检测功能的串口数据读取程序
# 配置通过命令行参数传入，格式：-p "COM7:12000000:D:\logs:test"

# --- 配置部分 ---
# DLL文件路径配置
# 注意：需要确保Python的架构(32位/64位)与DLL的架构匹配
if getattr(sys,'frozen',False):  # 检查是否是打包后的可执行文件
    current_dir = os.path.dirname(sys.executable)  # 如果是，获取可执行文件所在目录
else:
    current_dir = os.path.dirname(__file__)  # 否则获取脚本所在目录

# 定义DLL文件路径
DLL_PATH = os.path.join(current_dir, "SerialPortLib_x64.dll")  # 串口库DLL路径

# --- 全局变量定义 ---
# 使用线程安全的队列作为回调和写入线程之间的数据缓冲区
data_queue = queue.Queue(maxsize=4096)  # 限制队列大小，防止内存无限增长
# 定义停止写入线程的信号(使用None作为特殊标记)
stop_writer_signal = None

# 用于保存回调函数对象的引用，防止被Python垃圾回收机制回收
c_data_callback_instance = None  # 数据回调函数实例
c_error_callback_instance = None  # 错误回调函数实例

# 线程状态监控类
class WriterThreadStatus:
    def __init__(self):
        self.running = False        # 线程是否正在运行
        self.error = None           # 线程错误信息(如果有)
        self.bytes_written = 0      # 已写入的字节数统计
        self.last_heartbeat = 0     # 最后一次心跳时间戳
        self.lock = threading.Lock() # 线程安全锁，用于保护状态变量的访问

    def update(self, running=None, error=None, bytes_written=None):
        """更新线程状态(线程安全)"""
        with self.lock:  # 获取锁，确保线程安全
            if running is not None:
                self.running = running  # 更新运行状态
            if error is not None:
                self.error = error  # 更新错误信息
            if bytes_written is not None:
                self.bytes_written = bytes_written  # 更新写入字节数
            self.last_heartbeat = time.time()  # 更新心跳时间

    def get_status(self):
        """获取线程当前状态(线程安全)"""
        with self.lock:  # 获取锁，确保线程安全
            return {
                'running': self.running,  # 运行状态
                'error': self.error,      # 错误信息
                'bytes_written': self.bytes_written,  # 已写入字节数
                'last_heartbeat': self.last_heartbeat  # 最后心跳时间
            }

    def is_alive(self, timeout_seconds=5):
        """检查线程是否存活(基于心跳超时机制)"""
        with self.lock:  # 获取锁，确保线程安全
            # 判断线程是否在运行且心跳未超时
            return self.running and (time.time() - self.last_heartbeat) < timeout_seconds

# 创建全局的线程状态对象
writer_status = WriterThreadStatus()

# --- ctypes类型定义 ---
# 这部分定义了与C语言DLL交互所需的类型和函数原型

# 定义数据回调函数类型(必须与C头文件中的定义匹配)
# C语言原型: typedef void (*SerialDataCallback)(void* user_data, const char* buffer, size_t length);
DATA_CALLBACK_FUNC = ctypes.CFUNCTYPE(
    None,                    # 返回类型: void (在ctypes中用None表示)
    ctypes.c_void_p,         # 参数1: void* user_data (用户数据指针)
    ctypes.POINTER(ctypes.c_char), # 参数2: const char* buffer (字符缓冲区指针)
    ctypes.c_size_t          # 参数3: size_t length (数据长度)
)

# 定义错误回调函数类型
# C语言原型: typedef void (*SerialErrorCallback)(void* user_data, int error_code, const char* error_message);
ERROR_CALLBACK_FUNC = ctypes.CFUNCTYPE(
    None,                    # 返回类型: void
    ctypes.c_void_p,         # 参数1: void* user_data (用户数据指针)
    ctypes.c_int,            # 参数2: int error_code (错误代码)
    ctypes.c_char_p          # 参数3: const char* error_message (错误消息字符串)
)

# --- Python回调函数实现 ---

def py_data_callback(user_data_ptr, buffer_ptr, length):
    """
    C DLL调用的数据接收回调函数。
    将接收到的数据放入队列，尽可能快速地返回给DLL。
    """
    if buffer_ptr and length > 0:  # 检查指针和长度是否有效
        try:
            # 从C指针和长度创建Python bytes对象(这里会复制数据)
            # 使用切片方式比ctypes.string_at更Pythonic
            data_bytes = bytes(buffer_ptr[:length])  # 将C缓冲区数据转换为Python bytes

            # 将数据放入队列
            try:
                data_queue.put_nowait(data_bytes)  # 非阻塞方式放入队列
            except queue.Full:  # 如果队列已满
                print("警告：Python数据队列已满，可能丢失数据！", file=sys.stderr)
                # 可以考虑其他策略，如丢弃旧数据或扩展队列
        except Exception as e:  # 捕获所有异常
            print(f"错误：在数据回调中发生异常: {e}", file=sys.stderr)
            # 避免回调函数抛出未处理异常导致C++层崩溃

def py_error_callback(user_data_ptr, error_code, error_message_ptr):
    """C DLL调用的错误回调函数。"""
    try:
        # 解码错误消息(使用utf-8编码，错误时替换无效字符)
        error_message = error_message_ptr.decode('utf-8', errors='replace') if error_message_ptr else "N/A"
        print(f"[DLL ERROR] Code: {error_code}, Message: {error_message}", file=sys.stderr)
        # 可以根据错误代码执行特定操作，如设置停止标志
    except Exception as e:  # 捕获所有异常
         print(f"错误：在错误回调中发生异常: {e}", file=sys.stderr)

# --- 文件写入线程函数 ---
def writer_thread_func(filename):
    """从队列读取数据并写入二进制文件的线程函数。"""
    print(f"写入线程启动，将数据写入 '{filename}'")
    written_bytes_total = 0  # 已写入字节总数
    heartbeat_interval = 1.0  # 心跳间隔(秒)
    last_heartbeat = time.time()  # 上次心跳时间
    last_flush = time.time()  # 上次文件刷新时间
    flush_interval = 5.0  # 文件刷新间隔(秒)
    max_retry_count = 3  # 最大重试次数

    # 更新线程状态为运行中
    writer_status.update(running=True, bytes_written=0)

    try:
        # 尝试打开文件，带重试机制
        file_handle = None  # 文件句柄
        retry_count = 0  # 当前重试次数

        # 重试循环
        while retry_count < max_retry_count and file_handle is None:
            try:
                file_handle = open(filename, 'w', encoding='utf-8')  # 以文本写模式打开文件
            except IOError as e:  # 文件打开失败
                retry_count += 1  # 增加重试计数
                error_msg = f"错误：打开输出文件失败 (尝试 {retry_count}/{max_retry_count}): {e}"
                print(error_msg, file=sys.stderr)
                writer_status.update(error=error_msg)  # 更新线程状态

                if retry_count >= max_retry_count:  # 达到最大重试次数
                    raise IOError(f"无法打开输出文件，已达到最大重试次数: {e}")

                # 等待一段时间再重试
                time.sleep(1.0)

        # 使用with语句确保文件正确关闭
        with file_handle:
            while True:  # 主循环
                # 心跳检测
                current_time = time.time()
                if current_time - last_heartbeat >= heartbeat_interval:
                    writer_status.update(bytes_written=written_bytes_total)  # 更新状态
                    last_heartbeat = current_time  # 更新心跳时间

                # 定期刷新文件
                if current_time - last_flush >= flush_interval:
                    try:
                        file_handle.flush()  # 刷新文件缓冲区
                        last_flush = current_time  # 更新刷新时间
                    except IOError as e:  # 刷新失败
                        error_msg = f"警告：文件刷新失败: {e}"
                        print(error_msg, file=sys.stderr)
                        writer_status.update(error=error_msg)  # 更新状态

                try:
                    # 从队列获取数据，设置超时以允许检查退出信号
                    data_chunk = data_queue.get(timeout=0.5)  # 等待最多0.5秒

                    if data_chunk is stop_writer_signal:  # 检查停止信号
                        print("写入线程收到停止信号，正在退出...")
                        break  # 退出循环

                    if data_chunk:  # 如果有数据
                        try:
                            # 将二进制数据解码为字符串后写入文件
                            # 使用 errors='backslashreplace' 处理无法解码的字节
                            text_data = data_chunk.decode('utf-8', errors='backslashreplace')

                            # 按行处理，为每行添加时间戳
                            # 格式：2025-11-28 14:05:49 内容
                            lines = text_data.split('\n')
                            for i, line in enumerate(lines):
                                if i < len(lines) - 1:  # 不是最后一个元素，说明后面有换行符
                                    # 获取当前时间戳
                                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                                    timestamped_line = f"{timestamp} {line}\n"
                                    file_handle.write(timestamped_line)  # 写入文件
                                    print(timestamped_line, end='', flush=True)  # 显示到终端
                                elif line:  # 最后一个元素且不为空（说明数据不以换行结尾）
                                    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                                    timestamped_line = f"{timestamp} {line}"
                                    file_handle.write(timestamped_line)  # 写入文件
                                    print(timestamped_line, end='', flush=True)  # 显示到终端（不换行）
                                # 如果最后一个元素为空，说明数据以换行结尾，已经在上面处理过了

                            written_bytes_total += len(data_chunk)  # 更新写入字节数（原始字节数）
                            # 每写入1MB更新一次状态
                            if written_bytes_total % (1024 * 1024) == 0:
                                writer_status.update(bytes_written=written_bytes_total)
                                # 使用stderr输出状态信息，避免与log内容混淆
                                print(f"\n[状态] 已写入 {written_bytes_total / (1024*1024):.2f} MB", file=sys.stderr)
                        except IOError as e:  # 写入失败
                            error_msg = f"错误：文件写入失败: {e}"
                            print(error_msg, file=sys.stderr)
                            writer_status.update(error=error_msg)
                            raise  # 重新抛出异常，进入外层异常处理

                except queue.Empty:  # 队列为空
                    continue  # 继续循环等待
                except IOError as e:  # 文件IO错误
                    error_msg = f"错误：文件写入失败: {e}"
                    print(error_msg, file=sys.stderr)
                    writer_status.update(error=error_msg)

                    # 尝试重新打开文件
                    try:
                        file_handle.close()  # 关闭当前文件
                        file_handle = open(filename, 'a', encoding='utf-8')  # 以文本追加模式重新打开
                        print(f"已重新打开文件 '{filename}' 继续写入")
                    except IOError as reopen_error:  # 重新打开失败
                        error_msg = f"错误：无法重新打开文件: {reopen_error}"
                        print(error_msg, file=sys.stderr)
                        writer_status.update(error=error_msg)
                        break  # 无法恢复，退出线程
                except Exception as e:  # 其他未知异常
                    error_msg = f"错误：写入线程发生未知异常: {e}"
                    print(error_msg, file=sys.stderr)
                    writer_status.update(error=error_msg)
                    break  # 退出线程
    except Exception as e:  # 线程初始化失败
        error_msg = f"错误：写入线程初始化失败: {e}"
        print(error_msg, file=sys.stderr)
        writer_status.update(running=False, error=error_msg)
    finally:
        # 确保更新线程状态
        writer_status.update(running=False, bytes_written=written_bytes_total)
        print(f"写入线程结束。总共写入 {written_bytes_total} 字节。")

# --- 命令行参数解析函数 ---
def parse_arguments():
    """
    解析命令行参数，获取波特率、COM端口和输出文件路径。
    使用独立的命令行参数，避免Windows路径中冒号导致的解析问题。

    命令行格式：
      python log2bin_copy.py -c COM7 -b 12000000 -o D:\logs [-s test]

    参数说明：
      -c, --com-port    : 串口名称（如COM7）
      -b, --baud-rate   : 波特率（如12000000）
      -o, --output-dir  : 输出目录路径
      -s, --suffix      : 文件后缀名（可选）
    """
    # 创建命令行参数解析器
    parser = argparse.ArgumentParser(
        description='Log to Text Converter - 具备心跳检测功能的串口数据读取程序',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例用法：
  python log2bin_copy.py -c COM7 -b 12000000 -o D:\\logs
  python log2bin_copy.py -c COM7 -b 12000000 -o D:\\logs -s test
  python log2bin_copy.py --com-port COM3 --baud-rate 115200 --output-dir C:\\data --suffix debug

参数说明：
  -c, --com-port    : 串口名称，如 COM3, COM7 等
  -b, --baud-rate   : 波特率，如 9600, 115200, 12000000 等
  -o, --output-dir  : 数据文件保存的目录路径
  -s, --suffix      : 可选，添加到文件名中的标识符
        '''
    )

    # 添加串口参数（必需）
    parser.add_argument(
        "-c", "--com-port",
        required=True,
        metavar="PORT",
        help="串口名称，例如: COM3, COM7"
    )

    # 添加波特率参数（必需）
    parser.add_argument(
        "-b", "--baud-rate",
        required=True,
        type=int,
        metavar="RATE",
        help="波特率，例如: 9600, 115200, 12000000"
    )

    # 添加输出目录参数（必需）
    parser.add_argument(
        "-o", "--output-dir",
        required=True,
        metavar="DIR",
        help="输出目录路径，例如: D:\\logs"
    )

    # 添加后缀参数（可选）
    parser.add_argument(
        "-s", "--suffix",
        default="",
        metavar="SUFFIX",
        help="文件后缀名（可选），例如: test, debug"
    )

    # 解析命令行参数
    args = parser.parse_args()

    try:
        # --- 提取并验证串口名称 ---
        com_port = args.com_port.strip()
        if not com_port:
            raise ValueError("串口名不能为空")
        if not com_port.upper().startswith("COM"):
            raise ValueError(f"串口格式不正确，应为 COMx，得到的是 '{com_port}'")

        # --- 验证波特率 ---
        baud_rate = args.baud_rate
        if baud_rate <= 0:
            raise ValueError("波特率必须为正整数")

        # --- 验证输出目录 ---
        output_dir = args.output_dir.strip()
        if not output_dir:
            raise ValueError("输出目录路径不能为空")

        # 标准化路径 - 确保使用系统正确的路径分隔符
        output_dir = os.path.normpath(output_dir)

        # 确保输出目录存在
        try:
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
                print(f"已创建输出目录: {output_dir}")
        except Exception as e:
            raise ValueError(f"创建输出目录失败: {e}")

        # --- 获取后缀名 ---
        suffix = args.suffix.strip()

        # --- 生成基于时间戳的输出文件名 ---
        timestamp = time.strftime("%Y-%m-%d_%H%M%S")  # 当前时间格式化
        # 如果有后缀名，则添加到文件名中
        if suffix:
            output_file = os.path.join(output_dir, f"{timestamp}-{suffix}.log")
        else:
            output_file = os.path.join(output_dir, f"{timestamp}.log")

        # --- Windows串口名称处理 ---
        # 自动添加 \\.\\ 前缀以支持COM10及以上端口
        serial_port = com_port.upper()
        if sys.platform == "win32":
            try:
                # 提取COM后面的数字
                com_num = int(serial_port[3:])
                # 对于所有COM端口都添加前缀，确保稳定性
                serial_port = f"\\\\.\\{serial_port}"
                if com_num >= 10:
                    print(f"提示：自动添加了 '\\\\.\\' 前缀以支持 COM{com_num} 端口")
            except ValueError:
                # 如果COM后面不是数字，仍然添加前缀
                serial_port = f"\\\\.\\{serial_port}"

        # 返回串口名称、波特率和输出文件路径
        return serial_port, baud_rate, output_file

    except ValueError as e:
        print(f"错误：参数验证失败: {e}", file=sys.stderr)
        parser.print_help()
        input("Press Enter to exit...")
        sys.exit(1)
    except Exception as e:
        print(f"错误：解析命令行参数时发生未知错误: {e}", file=sys.stderr)
        input("Press Enter to exit...")
        sys.exit(1)

# --- 主程序入口 ---
if __name__ == "__main__":
    # 1. 解析命令行参数
    SERIAL_PORT, BAUD_RATE, OUTPUT_FILENAME = parse_arguments()
    print(f"配置信息：")
    print(f"  串口: {SERIAL_PORT}")
    print(f"  波特率: {BAUD_RATE} bps")
    print(f"  输出文件: {OUTPUT_FILENAME}")

    # 2. 检查并加载DLL
    if not os.path.exists(DLL_PATH):  # 检查DLL是否存在
        print(f"错误：找不到 DLL 文件: {DLL_PATH}")
        input("Press Enter to exit...")
        sys.exit(1)

    try:
        # 加载DLL(使用CDLL，适用于__cdecl调用约定)
        ser_lib = ctypes.CDLL(DLL_PATH)
        print(f"成功加载 DLL: {DLL_PATH}")
    except OSError as e:  # 加载失败
        print(f"错误：加载 DLL 失败: {e}", file=sys.stderr)
        input("Press Enter to exit...")
        sys.exit(1)

    # 3. 定义DLL导出函数的参数类型和返回类型
    try:
        # SerialPort_Open函数定义
        ser_lib.SerialPort_Open.argtypes = [
            ctypes.c_char_p,          # portName (串口名称)
            ctypes.c_ulong,# baudRate (波特率)
            DATA_CALLBACK_FUNC,       # dataCallback (数据回调函数)
            ERROR_CALLBACK_FUNC,      # errorCallback (错误回调函数)
            ctypes.c_void_p           # userData (用户数据指针)
        ]
        ser_lib.SerialPort_Open.restype = ctypes.c_int  # 返回类型为int

        # SerialPort_Close函数定义
        ser_lib.SerialPort_Close.argtypes = []  # 无参数
        ser_lib.SerialPort_Close.restype = ctypes.c_int  # 返回类型为int

        # SerialPort_Write函数定义(如果需要发送数据)
        ser_lib.SerialPort_Write.argtypes = [ctypes.c_char_p, ctypes.c_size_t]  # 参数: 数据指针, 数据长度
        ser_lib.SerialPort_Write.restype = ctypes.c_int  # 返回类型为int

        # SerialPort_IsOpen函数定义
        ser_lib.SerialPort_IsOpen.argtypes = []  # 无参数
        ser_lib.SerialPort_IsOpen.restype = ctypes.c_int  # 返回类型为int

    except AttributeError as e:  # 函数定义失败
        print(f"错误：DLL 中缺少必要的导出函数: {e}", file=sys.stderr)
        input("Press Enter to exit...")
        sys.exit(1)

    # 4. 创建回调函数实例并保持引用(防止被垃圾回收)
    c_data_callback_instance = DATA_CALLBACK_FUNC(py_data_callback)  # 数据回调实例
    c_error_callback_instance = ERROR_CALLBACK_FUNC(py_error_callback)  # 错误回调实例

    # 5. 启动写入线程
    writer_thread = threading.Thread(target=writer_thread_func, args=(OUTPUT_FILENAME,), daemon=True)
    # daemon=True表示主线程退出时自动结束写入线程(可能丢失缓冲数据)
    # 如果希望确保所有数据写入完成，不要设为daemon并确保主线程会join等待
    writer_thread.start()

    # 6. 打开串口
    print(f"正在打开串口 {SERIAL_PORT} @ {BAUD_RATE} bps...")
    # 将Python字符串编码为字节串传递给c_char_p
    port_name_bytes = SERIAL_PORT.encode('ascii')  # 或者utf-8，取决于DLL期望
    result = ser_lib.SerialPort_Open(
        port_name_bytes,  # 串口名称
        BAUD_RATE,  # 波特率
        c_data_callback_instance,  # 数据回调函数
        c_error_callback_instance,  # 错误回调函数
        None  # user_data, 可以传递需要回传给回调的Python对象指针(需额外处理)
    )

    if result != 0:  # 打开串口失败
        print(f"打开串口失败，错误码: {result}")
        # 尝试通知写入线程停止(虽然它可能还没写任何东西)
        data_queue.put(stop_writer_signal)
        writer_thread.join(timeout=2)  # 等待写入线程退出
        input("Press Enter to exit...")
        sys.exit(1)

    print("串口打开成功。正在接收数据...\n")
    print(f"数据将保存到 '{OUTPUT_FILENAME}'。按 Ctrl+C 停止。")

    # 7. 主循环 - 保持主线程运行，直到用户中断或发生错误
    try:
        check_interval = 5.0  # 主循环检查间隔(秒)
        heartbeat_timeout = 10.0  # 心跳超时时间(秒)
        last_status_check = time.time()  # 上次状态检查时间
        status_check_interval = 30.0  # 状态检查间隔(秒)

        while ser_lib.SerialPort_IsOpen():  # 串口保持打开状态时循环
            current_time = time.time()

            # 检查写入线程状态
            if not writer_thread.is_alive():  # 检查线程是否存活
                # 如果线程对象显示线程已终止，检查状态对象
                status = writer_status.get_status()
                if status['running']:  # 状态不一致
                    error_msg = f"错误：写入线程已终止，但状态显示仍在运行。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)
                    # 更新状态以反映线程已终止
                    writer_status.update(running=False, error=error_msg)
                    break
                elif data_queue.empty():  # 队列为空且线程终止
                    error_msg = f"错误：写入线程已终止。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)
                    break
                else:  # 队列不为空但线程终止
                    print("警告：写入线程已终止，但队列中还有数据。尝试重启写入线程...")
                    # 创建新的写入线程
                    writer_thread = threading.Thread(target=writer_thread_func, args=(OUTPUT_FILENAME,), daemon=True)
                    writer_thread.start()

            # 定期检查写入线程心跳
            if current_time - last_status_check >= status_check_interval:
                if not writer_status.is_alive(heartbeat_timeout):  # 心跳超时
                    status = writer_status.get_status()
                    error_msg = f"错误：写入线程心跳超时。最后错误: {status['error']}"
                    print(error_msg, file=sys.stderr)

                    # 尝试重启写入线程
                    if writer_thread.is_alive():  # 如果线程还在运行
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
                last_status_check = current_time  # 更新最后检查时间

            # 休眠一段时间
            time.sleep(check_interval)

    except KeyboardInterrupt:  # 捕获Ctrl+C中断
        print("\n收到 Ctrl+C，正在停止...")
    except Exception as e:  # 捕获其他异常
        print(f"\n主循环发生异常: {e}", file=sys.stderr)
    finally:  # 清理代码块
        # 8. 关闭串口
        print("正在关闭串口...")
        if ser_lib.SerialPort_IsOpen():  # 检查串口是否打开
            close_result = ser_lib.SerialPort_Close()  # 关闭串口
            if close_result != 0:  # 关闭失败
                print(f"关闭串口时发生错误，错误码: {close_result}", file=sys.stderr)
            else:  # 关闭成功
                print("串口已关闭。")

        # 9. 停止写入线程
        print("正在停止写入线程...")

        # 检查写入线程状态
        if writer_thread.is_alive():  # 线程仍在运行
            try:
                # 尝试正常停止
                data_queue.put(stop_writer_signal)  # 发送停止信号
                writer_thread.join(timeout=5)  # 等待线程完成，设置超时

                # 检查线程是否仍在运行
                if writer_thread.is_alive():
                    status = writer_status.get_status()
                    print(f"警告：写入线程在超时后仍未结束。状态: {status}", file=sys.stderr)

                    # 再次尝试停止
                    print("再次尝试停止写入线程...")
                    data_queue.put(stop_writer_signal)  # 再次发送停止信号
                    writer_thread.join(timeout=2)  # 等待短时间

                    if writer_thread.is_alive():  # 仍然无法停止
                        print("警告：无法正常停止写入线程，程序将继续退出。", file=sys.stderr)
                        # 不强制终止线程，因为不安全
            except Exception as e:  # 停止线程时发生异常
                print(f"关闭写入线程时发生异常: {e}", file=sys.stderr)
        else:  # 线程已经终止
            # 检查线程状态
            status = writer_status.get_status()
            if status['running']:  # 状态不一致
                print(f"警告：写入线程已终止，但状态显示仍在运行。最后错误: {status['error']}", file=sys.stderr)
                writer_status.update(running=False)  # 更新状态
            else:  # 状态一致
                print(f"写入线程已经结束。最后状态: 已写入 {status['bytes_written']} 字节")

        print("程序结束。")