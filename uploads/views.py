from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from django.conf import settings
import os
import json
import pwd
import grp

def get_quarcs_user():
    """获取quarcs用户的ID和组ID"""
    try:
        user_info = pwd.getpwnam('quarcs')
        user_id = user_info.pw_uid
        group_id = user_info.pw_gid
        return user_id, group_id
    except KeyError:
        # 如果找不到quarcs用户，尝试使用SUDO_USER或当前用户
        sudo_user = os.environ.get('SUDO_USER')
        if sudo_user:
            try:
                user_info = pwd.getpwnam(sudo_user)
                return user_info.pw_uid, user_info.pw_gid
            except KeyError:
                pass
        # 最后使用当前用户
        return os.getuid(), os.getgid()

def change_ownership(path, user_id, group_id, is_dir=False):
    """更改文件或目录的所有权"""
    try:
        os.chown(path, user_id, group_id)
        # 设置权限：目录为755，文件为644
        if is_dir:
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
        return True
    except (OSError, PermissionError) as e:
        # 如果更改权限失败（可能没有权限），记录但继续执行
        return False

def upload_file(request):
    if request.method == 'POST':
        files = request.FILES.getlist('file')  # 支持多文件上传
        
        if not files:
            return HttpResponse('Please select files to upload', status=400)
        
        # 确保目录存在并检查权限
        upload_dir = '/var/www/update_pack'
        try:
            os.makedirs(upload_dir, exist_ok=True)
            
            # 获取quarcs用户的ID和组ID
            user_id, group_id = get_quarcs_user()
            
            # 无论当前权限如何，都尝试将目录所有权改为quarcs用户
            change_ownership(upload_dir, user_id, group_id, is_dir=True)
            
            # 检查目录是否可写
            if not os.access(upload_dir, os.W_OK):
                return HttpResponse('Directory is not writable', status=500)
        except PermissionError:
            return HttpResponse('Permission denied to create directory', status=500)
        except Exception as e:
            # 如果更改权限失败，记录错误但继续
            pass
        
        saved_files = []
        errors = []
        
        for file in files:
            try:
                # 获取相对路径 - 多重保障机制
                # 方法1: 优先从POST数据中获取relative_path（客户端作为表单字段传递）
                relative_path = None
                if 'relative_path' in request.POST:
                    relative_path = request.POST.get('relative_path')
                    # 如果POST中有多个值（列表），取第一个
                    if isinstance(relative_path, list) and relative_path:
                        relative_path = relative_path[0]
                
                # 方法2: 如果POST中没有，从文件名中提取（文件名包含路径分隔符）
                if not relative_path and ('/' in file.name or '\\' in file.name):
                    relative_path = file.name
                
                # 方法3: 如果都没有，使用文件名（可能是单个文件，不包含路径）
                if not relative_path:
                    relative_path = file.name
                
                # 调试信息（可选，生产环境可以注释掉）
                # print(f"DEBUG: file.name={file.name}, relative_path={relative_path}, POST.relative_path={request.POST.get('relative_path', 'N/A')}")
                
                # 规范化路径：统一使用正斜杠，然后转换为系统分隔符
                # 这样可以保证跨平台兼容性
                relative_path = relative_path.replace('\\', '/').strip()
                
                # 清理路径，防止路径遍历攻击
                # 移除开头的斜杠和点号
                relative_path = relative_path.lstrip('/').lstrip('.')
                
                # 安全处理路径：移除路径遍历符号，但保持目录结构
                parts = []
                for part in relative_path.split('/'):
                    part = part.strip()  # 移除空白
                    if not part:  # 跳过空部分
                        continue
                    if part == '..':
                        # 遇到..，移除上一个部分（如果存在）- 防止路径遍历
                        if parts:
                            parts.pop()
                    elif part == '.':
                        # 忽略当前目录符号
                        continue
                    else:
                        # 有效的路径部分
                        parts.append(part)
                
                # 重新构建相对路径
                if parts:
                    relative_path = '/'.join(parts)
                    # 获取目录部分和文件名
                    if len(parts) > 1:
                        dir_part = '/'.join(parts[:-1])
                        filename = parts[-1]
                    else:
                        dir_part = None
                        filename = parts[0]
                else:
                    # 如果没有有效路径，使用原始文件名（不包含路径）
                    dir_part = None
                    filename = file.name if '/' not in file.name and '\\' not in file.name else os.path.basename(file.name)
                    relative_path = filename
                
                # 获取quarcs用户的ID和组ID
                user_id, group_id = get_quarcs_user()
                
                # 构建保存路径，保持文件夹结构
                if dir_part:
                    # 创建子目录结构
                    # 将正斜杠转换为系统分隔符
                    dir_part_normalized = dir_part.replace('/', os.sep)
                    sub_dir = os.path.join(upload_dir, dir_part_normalized)
                    os.makedirs(sub_dir, exist_ok=True)
                    
                    # 无论当前权限如何，都尝试将目录所有权改为quarcs用户
                    change_ownership(sub_dir, user_id, group_id, is_dir=True)
                    
                    save_path = os.path.join(sub_dir, filename)
                else:
                    # 没有子目录，直接保存到根目录
                    save_path = os.path.join(upload_dir, filename)
                
                # 写入文件
                with open(save_path, 'wb+') as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                
                # 无论当前权限如何，都尝试将文件所有权改为quarcs用户
                change_ownership(save_path, user_id, group_id, is_dir=False)
                
                saved_files.append({
                    'name': file.name,
                    'path': save_path,
                    'size': file.size
                })
                
            except Exception as e:
                errors.append({
                    'name': file.name,
                    'error': str(e)
                })
        
        # 构建响应消息
        if saved_files:
            message = f'Successfully saved {len(saved_files)} file(s):\n'
            for f in saved_files:
                message += f'  - {f["path"]}\n'
            if errors:
                message += f'\nFailed {len(errors)} file(s):\n'
                for e in errors:
                    message += f'  - {e["name"]}: {e["error"]}\n'
            return HttpResponse(message)
        else:
            error_msg = 'All files failed to save:\n'
            for e in errors:
                error_msg += f'  - {e["name"]}: {e["error"]}\n'
            return HttpResponse(error_msg, status=500)
                
    return render(request, 'uploads/upload.html')
