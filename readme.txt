1.此工具主要用于PCT挂测抓取UE日志(编译开源ACE库并封装串口通信所需接口)；
2.使用方法：
先在config.yaml配置工具的默认端口、波特率、日志文件名称后半部分、日志生成路径，期间工具抓取日志按“时间-日志文件名称后半部分”命名文件；例如log/2025-04-08_111238-3601PCT.bin
启动工具：log2bin_script.exe
关闭工具：taskkill log2bin_script.exe
