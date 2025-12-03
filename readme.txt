1.此工具主要用于PCT挂测抓取UE日志(编译开源ACE库并封装串口通信所需接口)；
2.使用方法：
先在config.yaml配置工具的默认端口、波特率、日志文件名称后半部分、日志生成路径，期间工具抓取日志按“时间-日志文件名称后半部分”命名文件；例如log/2025-04-08_111238-3601PCT.bin
启动工具：log2bin_script.exe
关闭工具：taskkill log2bin_script.exe

Note：
- bes_log_to_bin_script.py: 无心跳检测的串口读取程序；
- log2bin_script.py: 具备心跳检测的串口读取程序；
- log2bin_copy.py: 用于测试命令行跑log数据的，主要用于TT工具子进程调用获取C1log数据；

###########重要说明#################
根据Note中的提示，按需选择使用哪个py文件即可
生成可执行文件时，需要将build.py中的main_script修改为对应的py文件
