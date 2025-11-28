import ctypes
import sys
import os
import time
import queue
import threading
import argparse # 用于解析命令行参数

#无心跳检测的串口读取程序

# --- 配置 (现在大部分来自命令行) ---
# DLL 路径 (仍然需要配置或使其可发现)
DLL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SerialPortLib_x64.dll") # ! 修改为你的 DLL 实际路径

# --- 全局变量 ---
data_queue = queue.Queue(maxsize=4096) # 可以适当增大队列，缓冲更多数据
stop_writer_signal = None
c_data_callback_instance = None
c_error_callback_instance = None
g_stop_event = threading.Event() # 使用 Event 来优雅地停止主循环和通知错误

# --- ctypes 定义 (与之前相同) ---
# 定义回调函数类型
DATA_CALLBACK_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(ctypes.c_char), ctypes.c_size_t)
ERROR_CALLBACK_FUNC = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p)

# --- Python 回调函数实现 (与之前相同，增加了错误时设置停止事件) ---
def py_data_callback(user_data_ptr, buffer_ptr, length):
    if buffer_ptr and length > 0:
        try:
            data_bytes = bytes(buffer_ptr[:length])
            try:
                # 放入队列，如果满了则打印警告，避免阻塞回调太久
                data_queue.put_nowait(data_bytes)
            except queue.Full:
                print("警告：Python 数据队列已满，可能丢失数据！", file=sys.stderr)
        except Exception as e:
            print(f"错误：在数据回调中发生异常: {e}", file=sys.stderr)
            g_stop_event.set() # 发生未知错误时也停止

def py_error_callback(user_data_ptr, error_code, error_message_ptr):
    try:
        error_message = error_message_ptr.decode(sys.stdout.encoding or 'utf-8', errors='replace') if error_message_ptr else "N/A"
        print(f"[DLL ERROR] Code: {error_code}, Message: {error_message}", file=sys.stderr)
        # 可以在这里根据特定错误码判断是否是致命错误
        # if error_code == SOME_FATAL_ERROR_CODE:
        g_stop_event.set() # 任何 DLL 报告的错误都触发停止
    except Exception as e:
         print(f"错误：在错误回调中发生异常: {e}", file=sys.stderr)
         g_stop_event.set() # 发生未知错误时也停止

# --- 文件写入线程 (与之前基本相同) ---
def writer_thread_func(filename):
    print(f"写入线程启动，将数据写入 '{filename}'")
    written_bytes_total = 0
    file_opened = False
    try:
        # 延迟打开文件，直到收到第一块数据或确认路径有效
        f = None
        while not g_stop_event.is_set() or not data_queue.empty(): # 处理完队列再退出
            try:
                data_chunk = data_queue.get(timeout=0.1) # 短超时检查停止信号

                if data_chunk is stop_writer_signal:
                    print("写入线程收到停止信号，正在处理剩余数据...")
                    # 不需要 break，让循环自然结束处理完队列
                    continue # 继续检查队列

                if data_chunk:
                    # 第一次收到数据时打开文件
                    if not file_opened:
                        try:
                            # 确保目录存在
                            output_dir = os.path.dirname(filename)
                            if output_dir and not os.path.exists(output_dir):
                                os.makedirs(output_dir)
                                print(f"已创建目录: {output_dir}")
                            f = open(filename, 'wb')
                            file_opened = True
                        except IOError as e:
                            print(f"错误：无法打开或创建文件 '{filename}': {e}", file=sys.stderr)
                            g_stop_event.set() # 通知主线程停止
                            break # 无法写入，退出线程
                        except Exception as e:
                             print(f"错误：准备写入文件时发生异常: {e}", file=sys.stderr)
                             g_stop_event.set()
                             break

                    if f:
                        f.write(data_chunk)
                        written_bytes_total += len(data_chunk)
                        data_queue.task_done() # 标记任务完成 (虽然这里没用 join)

            except queue.Empty:
                # 队列为空，检查是否应停止
                if g_stop_event.is_set():
                    break # 如果主线程要求停止且队列空了，退出
                continue
            except Exception as e:
                print(f"错误：写入线程发生未知异常: {e}", file=sys.stderr)
                g_stop_event.set() # 通知主线程
                break
    finally:
        if f:
            f.close()
            print(f"文件 '{filename}' 已关闭。")
        print(f"写入线程结束。总共写入 {written_bytes_total} 字节。")

# --- 主程序入口 ---
def main():
    global c_data_callback_instance, c_error_callback_instance # 确保全局引用

    # 1. 设置命令行参数解析器
    parser = argparse.ArgumentParser(description="Log to Binary Converter。")
    parser.add_argument(
        "-p", "--port-config",
        required=True,
        help="串口配置字符串，格式为 '串口名:波特率:输出文件路径'，例如: 'COM3:12000000:C:\\log\\data.bin' 或 '/dev/ttyUSB0:12000000:/home/user/data.bin'. 推荐 Windows 串口名使用 \\\\.\\COMx 格式以支持 COM10 及以上端口。"
    )
    args = parser.parse_args()

    # 2. 解析配置字符串
    try:
        parts = args.port_config.split(':', 2) # 最多分 3 部分
        if len(parts) != 3:
            raise ValueError("配置字符串格式错误，需要 '串口名:波特率:文件路径'")

        serial_port = parts[0]
        baud_rate = int(parts[1])
        output_filename = parts[2]

        # 基本验证
        if not serial_port:
            raise ValueError("串口名不能为空")
        if baud_rate <= 0:
            raise ValueError("波特率必须为正整数")
        if not output_filename:
             raise ValueError("输出文件路径不能为空")

        # Windows下自动添加\\.\前缀
        if sys.platform == "win32":
            if serial_port.upper().startswith("COM"):
                try:
                    # 提取COM后面的数字
                    com_num = int(serial_port[3:])
                    if com_num >= 10 and not serial_port.startswith("\\\\.\\"):
                        serial_port = "\\\\.\\" + serial_port
                        print(f"提示：自动添加了 '\\\\.\\' 前缀以支持 COM{com_num} 端口")
                except ValueError:
                    # 如果COM后面不是数字，保持原样
                    pass
            elif not serial_port.startswith("\\\\.\\"):
                print(f"提示：Windows 串口名 '{serial_port}'未使用 '\\\\.\\' 前缀，可能在高负载下不稳定。推荐格式如 '\\\\.\\COM3'。")

    except ValueError as e:
        print(f"错误：解析配置字符串失败: {e}", file=sys.stderr)
        parser.print_help()
        sys.exit(1)
    except Exception as e:
        print(f"错误：解析命令行参数时发生未知错误: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"配置: 串口={serial_port}, 波特率={baud_rate}, 输出文件={output_filename}")

    # 3. 检查和加载 DLL
    if not os.path.exists(DLL_PATH):
        print(f"错误：找不到 DLL 文件: {DLL_PATH}", file=sys.stderr)
        sys.exit(1)

    try:
        ser_lib = ctypes.CDLL(DLL_PATH)
        print(f"成功加载 DLL: {DLL_PATH}")
    except OSError as e:
        print(f"错误：加载 DLL 失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 4. 定义导出函数的参数和返回类型
    try:
        ser_lib.SerialPort_Open.argtypes = [ctypes.c_char_p, ctypes.c_ulong, DATA_CALLBACK_FUNC, ERROR_CALLBACK_FUNC, ctypes.c_void_p]
        ser_lib.SerialPort_Open.restype = ctypes.c_int
        ser_lib.SerialPort_Close.argtypes = []
        ser_lib.SerialPort_Close.restype = ctypes.c_int
        ser_lib.SerialPort_Write.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        ser_lib.SerialPort_Write.restype = ctypes.c_int
        ser_lib.SerialPort_IsOpen.argtypes = []
        ser_lib.SerialPort_IsOpen.restype = ctypes.c_int
    except AttributeError as e:
        print(f"错误：DLL 中缺少必要的导出函数: {e}", file=sys.stderr)
        sys.exit(1)

    # 5. 创建回调函数实例并保持引用
    c_data_callback_instance = DATA_CALLBACK_FUNC(py_data_callback)
    c_error_callback_instance = ERROR_CALLBACK_FUNC(py_error_callback)

    # 6. 启动写入线程
    writer_thread = threading.Thread(target=writer_thread_func, args=(output_filename,))
    # 不设为 daemon，确保主线程会等待它结束
    writer_thread.start()

    # 7. 打开串口
    print(f"正在打开串口 {serial_port} @ {baud_rate} bps...")
    port_name_bytes = serial_port.encode('ascii') # 或者 'utf-8'
    result = ser_lib.SerialPort_Open(
        port_name_bytes,
        baud_rate,
        c_data_callback_instance,
        c_error_callback_instance,
        None
    )

    if result != 0:
        print(f"打开串口失败，错误码: {result}")
        g_stop_event.set() # 设置停止标志
        # 不需要向队列放 stop_writer_signal，写入线程会检查 g_stop_event
    else:
        print("串口打开成功。正在接收数据...")
        print(f"数据将保存到 '{output_filename}'。按 Ctrl+C 停止。")

    # 8. 主循环，等待停止信号
    try:
        while not g_stop_event.is_set():
            # 可以添加检查串口是否仍然打开的逻辑 (可选)
            # if result == 0 and not ser_lib.SerialPort_IsOpen():
            #    print("错误：DLL报告串口意外关闭。", file=sys.stderr)
            #    g_stop_event.set()
            time.sleep(0.2) # 等待停止事件，不需要太频繁

    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在停止...")
        g_stop_event.set() # 设置停止标志
    except Exception as e:
        print(f"\n主循环发生异常: {e}", file=sys.stderr)
        g_stop_event.set() # 设置停止标志
    finally:
        # 9. 关闭串口并清理
        print("正在关闭串口...")
        # 检查 ser_lib 是否已成功加载，以及端口是否可能已打开
        if 'ser_lib' in locals() and result == 0: # 只有成功打开才尝试关闭
             # 检查 IsOpen 可能不是绝对必要，Close 应该能处理已关闭情况
             # if ser_lib.SerialPort_IsOpen():
                close_result = ser_lib.SerialPort_Close()
                if close_result != 0:
                    print(f"关闭串口时发生错误，错误码: {close_result}", file=sys.stderr)
                else:
                    print("串口已关闭。")
             # else:
             #    print("串口已提前关闭或从未成功打开。")

        # 10. 等待写入线程完成
        print("等待写入线程完成所有缓冲数据...")
        # 不需要再向队列发送信号，写入线程会检查 g_stop_event
        writer_thread.join() # 等待写入线程自然结束

        print("程序结束。")

if __name__ == "__main__":
    main()