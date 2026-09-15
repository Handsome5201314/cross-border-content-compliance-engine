# -*- coding: utf-8 -*-
"""我的空间端点：统计 / 上传 / 文件列举 / 下载（§2.2 #17-20）。

安全：全部经 V2 storage 层（整数 user_id 拼路径 + safe_join 防穿越 +
secure_filename），原始文件名仅作展示，绝不进入路径。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from api.deps import get_current_user
from storage import space as storage_space

router = APIRouter(prefix="/api/space", tags=["space"])

_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20MB


def _dir_for_kind(kind: str, user_id: int):
    if kind == "inputs":
        return storage_space.inputs_dir(user_id)
    if kind == "outputs":
        return storage_space.outputs_dir(user_id)
    raise HTTPException(status_code=422, detail="kind 只支持 inputs | outputs")


@router.get("/stats")
def stats(user: dict = Depends(get_current_user)):
    uid = user["user_id"]
    uploads = storage_space.count_files(storage_space.inputs_dir(uid))
    outputs = storage_space.count_files(storage_space.outputs_dir(uid))
    return {"inputs": uploads + outputs, "uploads": uploads, "outputs": outputs}


@router.post("/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="不能上传空文件")
    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限")
    path = storage_space.save_upload(user["user_id"], file.filename or "file", data)
    return {"file_id": path.name, "name": file.filename, "size": len(data)}


@router.get("/files")
def files(kind: str = Query("inputs", pattern="^(inputs|outputs)$"),
          user: dict = Depends(get_current_user)):
    d = _dir_for_kind(kind, user["user_id"])
    rows = []
    if d.exists():
        for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if p.is_file():
                st = p.stat()
                rows.append({"name": p.name, "size": st.st_size,
                             "created_at": datetime.fromtimestamp(st.st_mtime)
                             .strftime("%Y-%m-%d %H:%M")})
    return {"files": rows}


@router.get("/file/{file_id}")
def download(file_id: str, kind: str = Query("inputs", pattern="^(inputs|outputs)$"),
             user: dict = Depends(get_current_user)):
    d = _dir_for_kind(kind, user["user_id"])
    try:
        path = storage_space.safe_join(d, file_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, filename=path.name)
