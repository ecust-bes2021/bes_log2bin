#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
打包脚本 - 使用PyInstaller打包应用程序为单个可执行文件
"""

import os
import sys
import subprocess
import platform
import shutil

# 设置应用程序版本号
VERSION = "1.0.1"

def main():
    """
    主函数 - 执行打包过程
    """
    print(f"开始打包应用程序 v{VERSION}...")
    
    # 检查是否安装了PyInstaller
    try:
        import PyInstaller
        print(f"PyInstaller 版本: {PyInstaller.__version__}")
    except ImportError:
        print("错误: 未安装PyInstaller。请先运行 'pip install pyinstaller' 安装。")
        return 1
    
    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 确定要打包的主脚本
    main_script = os.path.join(current_dir, "log2bin_script.py")
    if not os.path.exists(main_script):
        print(f"错误: 找不到主脚本 {main_script}")
        return 1
    
    # 确认DLL和配置文件的存在
    dll_path = os.path.join(current_dir, "SerialPortLib_x64.dll")
    config_path = os.path.join(current_dir, "config.yaml")
    
    if not os.path.exists(dll_path):
        print(f"错误: 找不到DLL文件 {dll_path}")
        return 1
    
    if not os.path.exists(config_path):
        print(f"错误: 找不到配置文件 {config_path}")
        return 1
    
    # 设置输出文件名（包含版本号）
    output_name = f"log2bin_script_v{VERSION}"
    
    # 构建PyInstaller命令
    cmd = [
        "pyinstaller",
        "--onefile",
        f"--name={output_name}",
        f"--add-binary={dll_path};.",
        f"--add-data={config_path};.",
        main_script
    ]
    
    # 执行打包命令
    print("执行打包命令:", " ".join(cmd))
    try:
        # 切换到当前目录执行命令，确保输出在正确的位置
        os.chdir(current_dir)
        subprocess.run(cmd, check=True)
        print("打包成功完成!")
        
        # 显示生成的可执行文件位置
        exe_suffix = ".exe" if platform.system() == "Windows" else ""
        exe_path = os.path.join("dist", f"{output_name}{exe_suffix}")
        
        if os.path.exists(exe_path):
            print(f"可执行文件已生成: {os.path.abspath(exe_path)}")
        else:
            print("警告: 未找到生成的可执行文件。")
        
        return 0
    except subprocess.CalledProcessError as e:
        print(f"错误: 打包过程失败! 错误代码: {e.returncode}")
        return e.returncode
    except Exception as e:
        print(f"错误: 打包过程失败! 异常: {str(e)}")
        return 1

if __name__ == "__main__":
    sys.exit(main()) 