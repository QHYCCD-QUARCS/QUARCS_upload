from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from django.conf import settings
import os
import errno
import json
import pwd
import grp
from pathlib import Path

# 默认上传根目录（未指定 -vue / -qt 时）
DEFAULT_UPLOAD_DIR = '/var/www/update_pack'
VUE_UPLOAD_DIR = '/var/www/html'
QT_UPLOAD_DIR = '/home/quarcs/workspace/QUARCS/QUARCS_QT-SeverProgram/src'


def _truthy(val):
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(val)


def _get_param(request, key):
    return request.POST.get(key) or request.GET.get(key)


def resolve_upload_base_dir(request):
    """
    根据请求参数选择上传根目录。
    支持：vue / qt（GET 或 POST，布尔或 upload_target=vue|qt）。
    同时指定 vue 与 qt 时返回错误信息。
    """
    ut = (_get_param(request, 'upload_target') or '').strip().lower()
    vue = _truthy(_get_param(request, 'vue')) or ut == 'vue'
    qt = _truthy(_get_param(request, 'qt')) or ut == 'qt'
    if vue and qt:
        return None, '不能同时指定 vue 与 qt 上传目标'
    if vue:
        return VUE_UPLOAD_DIR, None
    if qt:
        return QT_UPLOAD_DIR, None
    return DEFAULT_UPLOAD_DIR, None


def relax_upload_filter(request):
    """为真时关闭：隐藏路径段、目标路径软链、已存在多硬链文件的跳过逻辑。"""
    return _truthy(_get_param(request, 'relax_upload_filter'))


def path_has_hidden_segment(parts):
    return any(p.startswith('.') for p in parts if p)


def existing_chain_has_symlink(base_abs, target_abs):
    """从 base 到 target 的已存在路径上是否出现符号链接（不跟随解析）。"""
    base_abs = os.path.abspath(base_abs)
    target_abs = os.path.abspath(target_abs)
    if not target_abs.startswith(base_abs.rstrip(os.sep) + os.sep) and target_abs != base_abs:
        return True
    rel = os.path.relpath(target_abs, base_abs)
    cur = base_abs
    for part in Path(rel).parts:
        if part in ('.', ''):
            continue
        cur = os.path.join(cur, part)
        if os.path.lexists(cur) and os.path.islink(cur):
            return True
    return False


def is_under_real_base(base, candidate):
    try:
        base_r = os.path.realpath(base)
        cand_r = os.path.realpath(candidate)
        common = os.path.commonpath([base_r, cand_r])
    except (OSError, ValueError):
        return False
    return os.path.normcase(common) == os.path.normcase(base_r)


def existing_file_is_multi_hardlink(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return False
    try:
        return os.stat(path).st_nlink > 1
    except OSError:
        return False


def parse_uploaded_mode(request):
    """解析客户端上传的八进制权限位，如 0644 / 0755。"""
    raw_mode = (_get_param(request, 'file_mode') or '').strip()
    if not raw_mode:
        return None
    if len(raw_mode) > 4 or any(ch not in '01234567' for ch in raw_mode):
        return None
    try:
        return int(raw_mode, 8) & 0o7777
    except ValueError:
        return None


def is_build_client_binary_path(save_path):
    """是否为 BUILD/client 路径（需与 qt 上传模式配合使用，见 upload_file）。"""
    abs_path = os.path.abspath(save_path)
    base = os.path.basename(abs_path)
    parent = os.path.basename(os.path.dirname(abs_path))
    return base == 'client' and parent == 'BUILD'


def _executable_busy_errno_set():
    """Linux 上覆盖正在执行的文件会报 ETXTBSY；各平台 errno 可能不同。"""
    codes = set()
    for name in ('ETXTBSY', 'EBUSY'):
        if hasattr(errno, name):
            codes.add(getattr(errno, name))
    return codes


def write_client_binary_with_busy_fallback(save_path, file, user_id, group_id, uploaded_mode):
    """
    写入 BUILD/client。若目标正被运行中的进程占用导致无法覆盖（Linux ETXTBSY），
    则改为写入同目录下的 clientnew，由监控进程在重启 Qt 服务前完成替换。
    返回 (实际写入路径, 是否因占用而写入 clientnew)。
    """
    busy_errnos = _executable_busy_errno_set()
    content = b''.join(file.chunks())
    try:
        with open(save_path, 'wb') as destination:
            destination.write(content)
        change_ownership(save_path, user_id, group_id, is_dir=False, mode=uploaded_mode)
        return save_path, False
    except OSError as e:
        if not busy_errnos or e.errno not in busy_errnos:
            raise
    clientnew_path = os.path.join(os.path.dirname(save_path), 'clientnew')
    with open(clientnew_path, 'wb') as destination:
        destination.write(content)
    change_ownership(clientnew_path, user_id, group_id, is_dir=False, mode=uploaded_mode)
    return clientnew_path, True

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

def change_ownership(path, user_id, group_id, is_dir=False, mode=None):
    """更改文件或目录的所有权，并在可用时应用指定权限。"""
    success = True
    target_mode = mode if mode is not None else (0o755 if is_dir else 0o644)

    try:
        os.chown(path, user_id, group_id)
    except (OSError, PermissionError):
        success = False

    try:
        os.chmod(path, target_mode)
    except (OSError, PermissionError):
        success = False

    return success

def upload_file(request):
    if request.method == 'POST':
        files = request.FILES.getlist('file')  # 支持多文件上传
        
        if not files:
            return HttpResponse('Please select files to upload', status=400)

        upload_dir, dir_err = resolve_upload_base_dir(request)
        if dir_err:
            return HttpResponse(dir_err, status=400)
        # 仅 qt 上传目标（-qt / upload_target=qt）启用 BUILD/client 占用时写入 clientnew 的逻辑
        qt_upload_mode = upload_dir == QT_UPLOAD_DIR
        strict_path = not relax_upload_filter(request)
        
        # 确保目录存在并检查权限
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
        skipped = []
        uploaded_mode = parse_uploaded_mode(request)
        
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

                check_parts = parts if parts else ([filename] if filename else [])
                if strict_path and path_has_hidden_segment(check_parts):
                    skipped.append({
                        'name': file.name,
                        'reason': '路径包含隐藏目录或隐藏文件名',
                    })
                    continue
                
                # 获取quarcs用户的ID和组ID
                user_id, group_id = get_quarcs_user()
                
                # 构建保存路径，保持文件夹结构
                if dir_part:
                    dir_part_normalized = dir_part.replace('/', os.sep)
                    sub_dir = os.path.join(upload_dir, dir_part_normalized)
                    save_path = os.path.join(sub_dir, filename)
                else:
                    sub_dir = upload_dir
                    save_path = os.path.join(upload_dir, filename)

                if strict_path:
                    if existing_chain_has_symlink(upload_dir, save_path):
                        skipped.append({
                            'name': file.name,
                            'reason': '路径上存在符号链接',
                        })
                        continue
                    if os.path.lexists(save_path):
                        if os.path.islink(save_path):
                            skipped.append({
                                'name': file.name,
                                'reason': '目标为符号链接，跳过覆盖',
                            })
                            continue
                        if existing_file_is_multi_hardlink(save_path):
                            skipped.append({
                                'name': file.name,
                                'reason': '目标文件存在多个硬链接，跳过覆盖',
                            })
                            continue

                if dir_part:
                    os.makedirs(sub_dir, exist_ok=True)
                    if not is_under_real_base(upload_dir, sub_dir):
                        errors.append({
                            'name': file.name,
                            'error': '拒绝写入：路径已逃逸出上传根目录',
                        })
                        continue
                    change_ownership(sub_dir, user_id, group_id, is_dir=True)

                if not is_under_real_base(upload_dir, save_path):
                    errors.append({
                        'name': file.name,
                        'error': '拒绝写入：目标路径不在上传根目录内',
                    })
                    continue
                
                # BUILD/client：若进程占用无法原地覆盖，则写入 clientnew，由 quarcsmonitor 重启前替换
                if qt_upload_mode and is_build_client_binary_path(save_path):
                    try:
                        actual_path, staged_new = write_client_binary_with_busy_fallback(
                            save_path, file, user_id, group_id, uploaded_mode)
                    except Exception as e:
                        errors.append({
                            'name': file.name,
                            'error': str(e),
                        })
                        continue
                    entry = {
                        'name': file.name,
                        'path': actual_path,
                        'size': file.size,
                    }
                    if staged_new:
                        entry['note'] = 'client 正被占用，已暂存为 clientnew，将在重启 Qt 服务后生效'
                    saved_files.append(entry)
                    continue

                # 写入文件（wb 截断，同名普通文件覆盖）
                with open(save_path, 'wb') as destination:
                    for chunk in file.chunks():
                        destination.write(chunk)
                
                # 无论当前权限如何，都尝试将文件所有权改为quarcs用户
                change_ownership(save_path, user_id, group_id, is_dir=False, mode=uploaded_mode)
                
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
        lines = []
        if saved_files:
            lines.append(f'成功保存 {len(saved_files)} 个文件:')
            for f in saved_files:
                line = f'  - {f["path"]}'
                if f.get('note'):
                    line += f' ({f["note"]})'
                lines.append(line)
        if skipped:
            lines.append(f'跳过 {len(skipped)} 个文件:')
            for s in skipped:
                lines.append(f'  - {s["name"]}: {s["reason"]}')
        if errors:
            lines.append(f'失败 {len(errors)} 个文件:')
            for e in errors:
                lines.append(f'  - {e["name"]}: {e["error"]}')
        message = '\n'.join(lines) if lines else '无处理结果'

        if saved_files:
            return HttpResponse(message)
        if errors:
            return HttpResponse(message, status=500)
        if skipped:
            return HttpResponse(message)
        return HttpResponse(message, status=500)
                
    return render(request, 'uploads/upload.html')
