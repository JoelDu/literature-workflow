import os
import time
import uuid
import requests
import zipfile
from pathlib import Path
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


class MinerUClient:
    def __init__(self, api_keys: str):
        # 支持逗号分隔的多个 key
        self.api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        if not self.api_keys:
            raise ValueError("MINERU_API_KEY 未配置；解析 PDF/教材前请在环境中提供有效 Key")
        # 优先使用第二个 key (索引为 1) 进行文献处理，第一个 key 作为后备保障
        self.current_key_idx = 1 if len(self.api_keys) > 1 else 0
        self.base_url = "https://mineru.net/api/v4"
        self._update_headers()

    def _update_headers(self):
        self.api_key = self.api_keys[self.current_key_idx]
        self.headers = {"Authorization": f"Bearer {self.api_key}"}

    def rotate_key(self):
        if len(self.api_keys) > 1:
            self.current_key_idx = (self.current_key_idx + 1) % len(self.api_keys)
            self._update_headers()
            print(f"[MinerU Client] 自动切换至备用 Key (序号: {self.current_key_idx})")

    def process_pdf(self, pdf_path: str, output_dir: str, poll_interval: int = 10, timeout: int = 600) -> dict:
        """
        处理单篇 PDF：
        - 自动循环重试所有配置的 MinerU API Keys
        """
        filename = os.path.basename(pdf_path)
        data_id = str(uuid.uuid4())
        os.makedirs(output_dir, exist_ok=True)

        last_error = None
        # 对每一个 key 尝试一遍，最多运行 len(self.api_keys) 次
        for attempt in range(len(self.api_keys)):
            try:
                # Step 1: 获取上传链接
                upload_req = {
                    "files": [{"name": filename, "data_id": data_id}],
                    "model_version": "vlm",
                    "is_json_md": True,
                    "enable_formula": True,
                    "enable_table": True,
                    # 让 MinerU 服务端把所有支持的格式都渲染出来一并取回本地
                    # （docx 供阅读、html/latex 备用），硬盘充足、按页计费不因此变。
                    # 默认已含 markdown+json；这里补足 docx/html/latex 三种。
                    "extra_formats": ["docx", "html", "latex"],
                }
                
                @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8), retry=retry_if_exception_type(requests.RequestException), reraise=True)
                def get_upload_link():
                    return requests.post(
                        f"{self.base_url}/file-urls/batch",
                        headers=self.headers,
                        json=upload_req,
                        timeout=30,
                    )

                resp1 = get_upload_link()
                # 检查额度或认证错误 (401, 402, 403, 429)
                if resp1.status_code in (401, 402, 403, 429):
                    raise RuntimeError(f"额度不足/鉴权失效 (HTTP {resp1.status_code})")
                    
                resp1.raise_for_status()
                data1 = resp1.json()
                if data1.get("code") != 0:
                    err_msg = data1.get("description") or data1.get("msg") or str(data1)
                    raise RuntimeError(f"接口返回异常 (Code {data1.get('code')}): {err_msg}")

                batch_id = data1["data"]["batch_id"]
                upload_url = data1["data"]["file_urls"][0]

                # Step 2: 上传文件
                @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8), retry=retry_if_exception_type(requests.RequestException), reraise=True)
                def upload_file():
                    with open(pdf_path, "rb") as f:
                        return requests.put(upload_url, data=f, headers={"Content-Type": ""}, timeout=120)
                        
                resp2 = upload_file()
                if resp2.status_code != 200:
                    raise RuntimeError(f"OSS 上传失败 (HTTP {resp2.status_code})")

                # Step 3: 轮询结果
                status_url = f"{self.base_url}/extract-results/batch/{batch_id}"
                
                @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=8), retry=retry_if_exception_type(requests.RequestException), reraise=True)
                def check_status():
                    return requests.get(status_url, headers=self.headers, timeout=30)
                    
                elapsed = 0
                while elapsed < timeout:
                    time.sleep(poll_interval)
                    elapsed += poll_interval

                    resp4 = check_status()
                    if resp4.status_code in (401, 403):
                        raise RuntimeError(f"轮询中鉴权失效 (HTTP {resp4.status_code})")

                    resp4_json = resp4.json()
                    result_list = resp4_json.get("data", {}).get("extract_result", [])
                    if not result_list:
                        continue

                    task_info = result_list[0]
                    state = task_info.get("state", "")

                    if state == "done":
                        return self._download_and_extract(task_info, output_dir)
                    elif state == "failed":
                        raise RuntimeError(f"解析失败: {task_info.get('err_msg', task_info)}")

                raise TimeoutError(f"解析超时 ({timeout}s): {pdf_path}")

            except Exception as e:
                last_error = e
                # 如果还有备用 key，则切换并重试整个 process_pdf 流程
                if attempt < len(self.api_keys) - 1:
                    print(f"[MinerU Client] 当前 Key (序号: {self.current_key_idx}) 请求失败: {e}。正在自动切换至备用 Key...")
                    self.rotate_key()
                    continue
                else:
                    raise RuntimeError(f"所有配置的 MinerU API Keys 均尝试失败。最后一次报错: {last_error}")

    def _download_and_extract(self, task_info: dict, output_dir: str) -> dict:
        """下载解析结果 zip 包并解压，返回 markdown 内容和资源路径。"""
        full_zip_url = task_info.get("full_zip_url")
        markdown_content = task_info.get("markdown", "")

        if full_zip_url:
            zip_path = os.path.join(output_dir, "_result.zip")
            
            @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=4, max=10), retry=retry_if_exception_type(requests.RequestException), reraise=True)
            def download_zip():
                return requests.get(full_zip_url, stream=True, timeout=120)
                
            resp = download_zip()
            resp.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(output_dir)
            os.remove(zip_path)

            # 找最大的 md 文件作为主内容
            md_files = list(Path(output_dir).rglob("*.md"))
            if md_files:
                md_file = max(md_files, key=lambda f: f.stat().st_size)
                markdown_content = md_file.read_text(encoding="utf-8")

            # 找 docx 文件
            docx_files = list(Path(output_dir).rglob("*.docx"))
            if docx_files:
                docx_file = max(docx_files, key=lambda f: f.stat().st_size)
                docx_path = str(docx_file.absolute())

        images_dir = os.path.join(output_dir, "images")
        return {
            "status": "success",
            "markdown": markdown_content,
            "output_dir": output_dir,
            "images_dir": images_dir if os.path.exists(images_dir) else output_dir,
            "docx_path": locals().get("docx_path"),
        }
