import asyncio
import logging
import re
import time
import base64
from urllib.parse import urlparse, urljoin, urlencode

import aiohttp
from bs4 import BeautifulSoup, SoupStrainer

from config import FLARESOLVERR_URL, FLARESOLVERR_TIMEOUT, get_proxy_for_url, TRANSPORT_ROUTES, get_solver_proxy_url, GLOBAL_PROXIES
from utils.cookie_cache import CookieCache
from utils.solver_manager import solver_manager

logger = logging.getLogger(__name__)

class ExtractorError(Exception):
    pass

class Settings:
    flaresolverr_url = FLARESOLVERR_URL
    flaresolverr_timeout = FLARESOLVERR_TIMEOUT

settings = Settings()

class DeltabitExtractor:
    _result_cache = {} # cache for final results: {url: (result, timestamp)}

    def __init__(self, request_headers: dict = None, proxies: list = None, bypass_warp: bool = False):
        self.request_headers = request_headers or {}
        self.base_headers = self.request_headers.copy()
        if "User-Agent" not in self.base_headers and "user-agent" not in self.base_headers:
             self.base_headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.proxies = proxies or GLOBAL_PROXIES
        self.cache = CookieCache("universal")
        self.mediaflow_endpoint = "proxy_stream_endpoint"
        self.bypass_warp_active = bypass_warp
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers=self.base_headers)
        return self.session

    async def _request_flaresolverr(self, cmd: str, url: str = None, post_data: str = None, session_id: str = None, wait: int = 0, headers: dict | None = None) -> dict:
        endpoint = f"{settings.flaresolverr_url.rstrip('/')}/v1"
        payload = {"cmd": cmd, "maxTimeout": (settings.flaresolverr_timeout + 60) * 1000}
        if wait > 0: payload["wait"] = wait
        fs_headers = {}
        if url: 
            payload["url"] = url
            proxy = get_proxy_for_url(url, TRANSPORT_ROUTES, self.proxies, bypass_warp=self.bypass_warp_active)
            if proxy:
                payload["proxy"] = {"url": proxy}
                fs_headers["X-Proxy-Server"] = get_solver_proxy_url(proxy)
        if post_data: payload["postData"] = post_data
        if session_id: payload["session"] = session_id
        if headers: payload["headers"] = headers
        async with aiohttp.ClientSession() as fs_session:
            async with fs_session.post(endpoint, json=payload, headers=fs_headers, timeout=settings.flaresolverr_timeout + 95) as resp:
                data = await resp.json()
        if data.get("status") != "ok": raise ExtractorError(f"FlareSolverr: {data.get('message')}")
        return data

    def _step_headers(self, ua: str, referer: str | None = None) -> dict:
        headers = {"User-Agent": ua}
        if referer:
            headers["Referer"] = referer
        return headers

    async def extract(self, url: str, **kwargs) -> dict:
        # Normalize URL for cache
        normalized_url = url.strip()
        # Check cache (10 minutes validity)
        if normalized_url in DeltabitExtractor._result_cache:
            res, ts = DeltabitExtractor._result_cache[normalized_url]
            if time.time() - ts < 600:
                logger.info(f"🚀 [Cache Hit] Using cached extraction result for: {normalized_url}")
                return res
        
        logger.info(f"🔍 [Cache Miss] Extracting new link for: {normalized_url}")
        proxy = get_proxy_for_url(normalized_url, TRANSPORT_ROUTES, self.proxies, self.bypass_warp_active)
        is_redirector_url = any(d in normalized_url.lower() for d in ["safego.cc", "clicka.cc", "clicka"])
        redirect_session_id = await solver_manager.get_persistent_session("redirector:clicka-safego", proxy) if is_redirector_url else None
        final_session_id = await solver_manager.get_persistent_session("deltabit", proxy)
        session_id = redirect_session_id or final_session_id
        is_persistent = True # Always persistent for this key
        try:
            ua, cookies = self.base_headers.get("User-Agent"), {}
            # 1. Hybrid Solver for Redirector (FAST)
            if is_redirector_url:
                url, ua, cookies = await self._solve_redirector_hybrid(url, session_id)

            session_id = final_session_id

            if "deltabit.co" in url.lower(): url = url.replace("deltabit.co/ ", "deltabit.co/")
            
            # 2. Final page fetch (FlareSolverr for stability)
            res = await self._request_flaresolverr("request.get", url, session_id=session_id, wait=2000, headers=self._step_headers(ua, url))
            solution = res.get("solution", {})
            html, ua = solution.get("response", ""), solution.get("userAgent", self.base_headers.get("User-Agent"))
            # Collect final cookies
            fs_cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
            cookies.update(fs_cookies)
            url = solution.get("url", url) # Update URL to final destination
            
            soup = BeautifulSoup(html, 'lxml')
            form_data = {inp.get('name'): inp.get('value', '') for inp in soup.find_all('input') if inp.get('name')}
            if not form_data.get("op"):
                link_match = re.search(r'sources:\s*\["([^"]+)"', html) or re.search(r'file:\s*["\']([^"\']+)["\']', html)
                if link_match: 
                    result = self._build_result(link_match.group(1), url, ua, proxy, cookies=cookies)
                    DeltabitExtractor._result_cache[normalized_url] = (result, time.time())
                    logger.info("✅ Extraction success (direct source found)")
                    return result
                raise ExtractorError("Deltabit: Form not found")

            # 3. Final POST via FlareSolverr (STABLE)
            form_data['imhuman'], form_data['referer'] = "", url
            await asyncio.sleep(2.5) 
            
            post_res = await self._request_flaresolverr("request.post", url, urlencode(form_data), session_id=session_id, wait=0, headers=self._step_headers(ua, url))
            post_solution = post_res.get("solution", {})
            post_html = post_solution.get("response", "")
            # Update cookies after POST
            cookies.update({c["name"]: c["value"] for c in post_solution.get("cookies", [])})

            link_match = re.search(r'sources:\s*\["([^"]+)"', post_html) or re.search(r'file:\s*["\']([^"\']+)["\']', post_html)
            if not link_match: raise ExtractorError("Deltabit: Video source not found")
            result = self._build_result(link_match.group(1), url, ua, proxy, cookies=cookies)
            DeltabitExtractor._result_cache[normalized_url] = (result, time.time())
            return result
        finally:
            if redirect_session_id:
                await solver_manager.release_session(redirect_session_id, is_persistent)
            if final_session_id and final_session_id != redirect_session_id:
                await solver_manager.release_session(final_session_id, is_persistent)

    async def _solve_redirector_hybrid(self, url: str, session_id: str) -> tuple:
        res = await self._request_flaresolverr("request.get", url, session_id=session_id, headers=self._step_headers(self.base_headers.get("User-Agent"), url))
        solution = res.get("solution", {})
        ua, cookies = solution.get("userAgent"), {c["name"]: c["value"] for c in solution.get("cookies", [])}
        html, current_url = solution.get("response", ""), solution.get("url", url)
        headers, session = self._step_headers(ua, url), await self._get_session()
        fs_counter = 0
        max_fs_calls = 25
        use_flaresolverr_only = True

        async def light_fetch(target_url, post_data=None, referer=None, force_flaresolverr=False):
            nonlocal fs_counter
            request_headers = dict(headers)
            if referer:
                request_headers["Referer"] = referer
            if force_flaresolverr:
                if fs_counter >= max_fs_calls:
                    logger.warning(f"Deltabit: FlareSolverr call limit reached ({max_fs_calls})")
                    return None, target_url
                fs_counter += 1
                try:
                    fs_cmd = "request.post" if post_data else "request.get"
                    fs_res = await self._request_flaresolverr(fs_cmd, target_url, urlencode(post_data) if post_data else None, session_id=session_id, headers=request_headers)
                    sol = fs_res.get("solution", {})
                    cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                    return sol.get("response", ""), sol.get("url", target_url)
                except Exception:
                    return None, target_url
            try:
                if post_data:
                    async with session.post(target_url, data=post_data, cookies=cookies, headers=request_headers, timeout=12) as r:
                        text = await r.text()
                        if r.status != 200 or "cf-challenge" in text or "ray id" in text.lower() or "checking your browser" in text.lower():
                            if fs_counter >= max_fs_calls:
                                logger.warning(f"Deltabit: FlareSolverr call limit reached ({max_fs_calls})")
                                return text, str(r.url)
                            fs_counter += 1
                            logger.info(f"Cloudflare or error ({r.status}) detected in redirect step (POST) for {target_url}, using FlareSolverr...")
                            fs_res = await self._request_flaresolverr("request.post", target_url, urlencode(post_data), session_id=session_id, headers=request_headers)
                            sol = fs_res.get("solution", {})
                            cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                            return sol.get("response", ""), sol.get("url", target_url)
                        return text, str(r.url)
                else:
                    async with session.get(target_url, cookies=cookies, headers=request_headers, timeout=12) as r:
                        text = await r.text()
                        if r.status != 200 or "cf-challenge" in text or "ray id" in text.lower() or "checking your browser" in text.lower():
                            if fs_counter >= max_fs_calls:
                                logger.warning(f"Deltabit: FlareSolverr call limit reached ({max_fs_calls})")
                                return text, str(r.url)
                            fs_counter += 1
                            logger.info(f"Cloudflare or error ({r.status}) detected in redirect step (GET) for {target_url}, using FlareSolverr...")
                            fs_res = await self._request_flaresolverr("request.get", target_url, session_id=session_id, headers=request_headers)
                            sol = fs_res.get("solution", {})
                            cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                            return sol.get("response", ""), sol.get("url", target_url)
                        return text, str(r.url)
            except Exception as e:
                logger.debug(f"Light fetch failed for {target_url}: {e}, falling back to FlareSolverr...")
                try:
                    fs_counter += 1
                    fs_cmd = "request.post" if post_data else "request.get"
                    fs_res = await self._request_flaresolverr(fs_cmd, target_url, urlencode(post_data) if post_data else None, session_id=session_id, headers=request_headers)
                    sol = fs_res.get("solution", {})
                    cookies.update({c["name"]: c["value"] for c in sol.get("cookies", [])})
                    return sol.get("response", ""), sol.get("url", target_url)
                except: return None, target_url

        async def binary_fetch(target_url):
            """Fetch binary data (like images) with direct/FlareSolverr hybrid fallback."""
            nonlocal fs_counter
            request_headers = dict(headers)
            request_headers["Referer"] = current_url
            try:
                async with session.get(target_url, cookies=cookies, headers=request_headers, timeout=12) as r:
                    if r.status == 200:
                        return await r.read()
                    logger.info(f"Direct binary fetch failed ({r.status}) for {target_url}, trying FlareSolverr...")
            except Exception as e:
                logger.debug(f"Direct binary fetch error: {e}")

            if fs_counter < max_fs_calls:
                fs_counter += 1
                try:
                    fs_res = await self._request_flaresolverr("request.get", target_url, session_id=session_id, headers=request_headers)
                    solution = fs_res.get("solution", {})
                    # FlareSolverr returns response as a string. If it's an image, it might be base64 or raw.
                    # Usually FlareSolverr doesn't handle binary well unless it's base64 in JSON.
                    # But if we used 'request.get', FS might return the raw response if it's not HTML.
                    response_text = solution.get("response", "")
                    # Check if it looks like base64
                    if "base64" in response_text or len(response_text) > 1000:
                         # Attempt to decode if it's potentially base64
                         try: return base64.b64decode(response_text)
                         except: return response_text.encode('utf-8')
                    return response_text.encode('utf-8')
                except: pass
            return None

        for step in range(8):
            if not any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]): break
            
            soup = BeautifulSoup(html, "lxml")
            
            # 1. Handle CAPTCHA if present
            img_tag = soup.find("img", src=re.compile(r'data:image/png;base64,|captcha\.php'))
            if img_tag:
                logger.info(f"🧩 Numeric captcha detected on {current_url[:40]}...")
                import ddddocr
                ocr = ddddocr.DdddOcr(show_ad=False)
                captcha_data = None
                if "base64," in img_tag["src"]:
                    try: captcha_data = base64.b64decode(img_tag["src"].split(",")[1])
                    except: pass
                else:
                    # Download image using robust binary_fetch
                    captcha_data = await binary_fetch(urljoin(current_url, img_tag["src"]))
                
                if captcha_data:
                    captcha = re.sub(r'[^0-9]', '', ocr.classification(captcha_data)).replace('o','0').replace('l','1')
                    logger.info(f"🤖 OCR Prediction: {captcha}")
                    form = soup.find("form")
                    post_fields = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")} if form else {}
                    for key in ["code", "captch5", "captcha"]:
                        if key in post_fields or (form and form.find("input", {"name": key})):
                            post_fields[key] = captcha
                            break
                    else: post_fields["code"] = captcha
                    
                    # Submit captcha
                    await asyncio.sleep(3.0) # Wait a bit to satisfy anti-bot timers
                    html, current_url = await light_fetch(current_url, post_data=post_fields, referer=current_url, force_flaresolverr=use_flaresolverr_only)
                    if not html: break
                    soup = BeautifulSoup(html, "lxml")
                    headers["Referer"] = current_url
                    if current_url and any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]):
                        use_flaresolverr_only = True
                    logger.info(f"✅ Captcha submitted, current URL: {current_url}")
                    
                    # NEW: Check if captcha is still there (submission failed)
                    if soup.find("img", src=re.compile(r'data:image/png;base64,|captcha\.php')):
                        logger.warning("⚠️ Captcha still present after submission, retrying solver...")
                        continue # Re-run step loop to solve again
                else:
                    logger.warning("❌ Failed to download captcha image.")

            # 2. Handle "Step" buttons and "Proceed" buttons
            next_url = None
            button_markers = ["proceed", "continue", "prosegui", "avanti", "click here", "clicca qui", "step", "passaggio", "vai al"]
            
            for attempt in range(15):
                meta_refresh = soup.find("meta", attrs={"http-equiv": "refresh"})
                if meta_refresh and "url=" in meta_refresh.get("content", "").lower():
                    next_url = urljoin(current_url, meta_refresh["content"].lower().split("url=")[1].strip())
                    break

                for a_tag in soup.find_all(["a", "button", "div", "input"], href=True) or soup.find_all(["a", "button", "div", "input"]):
                    txt = a_tag.get_text().strip().lower()
                    if not txt:
                        # Check value or title attribute for inputs/buttons
                        txt = (a_tag.get("value") or a_tag.get("title") or "").strip().lower()
                    
                    if any(x in txt for x in button_markers):
                        href = a_tag.get("href")
                        if not href:
                            onclick = a_tag.get("onclick", "")
                            oc_match = re.search(r'location\.href\s*=\s*["\']([^"\']+)["\']', onclick)
                            if oc_match: href = oc_match.group(1)

                        if href:
                            next_url = urljoin(current_url, href)
                            break
                        elif a_tag.name in ["button", "input"] and (a_tag.get("type") == "submit" or a_tag.name == "button"):
                            # Find parent form
                            form = a_tag.find_parent("form")
                            if form:
                                logger.info(f"📝 Submitting form found via button: {txt}")
                                post_url = urljoin(current_url, form.get("action", ""))
                                post_data = {inp.get("name"): inp.get("value", "") for inp in form.find_all("input") if inp.get("name")}
                                html, current_url = await light_fetch(post_url, post_data=post_data, referer=current_url, force_flaresolverr=use_flaresolverr_only)
                                if html:
                                    soup = BeautifulSoup(html, "lxml")
                                    headers["Referer"] = current_url
                                    if current_url and any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]):
                                        use_flaresolverr_only = True
                                    # Form submitted, check for next step in new HTML
                                    # Re-trigger attempt loop with new soup? 
                                    # Actually, setting next_url to something non-None will break the inner loop
                                    next_url = current_url 
                                    break
                
                if next_url and next_url != current_url and "uprot.net" not in next_url:
                    previous_url = current_url
                    current_url = next_url
                    html, current_url = await light_fetch(current_url, referer=previous_url, force_flaresolverr=use_flaresolverr_only)
                    if html:
                        soup = BeautifulSoup(html, "lxml")
                        headers["Referer"] = previous_url
                        if current_url and any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]):
                            use_flaresolverr_only = True
                    break
                
                if attempt < 6: # Reduced attempts, but longer wait
                    await asyncio.sleep(4.0) 
                    html, current_url = await light_fetch(current_url, referer=current_url, force_flaresolverr=use_flaresolverr_only)
                    if html:
                        soup = BeautifulSoup(html, "lxml")
                        headers["Referer"] = current_url
                        if current_url and any(d in current_url.lower() for d in ["safego.cc", "clicka.cc", "clicka", "uprot.net"]):
                            use_flaresolverr_only = True
                else:
                    break
            
            if not next_url:
                # Check for automatic redirect in HTML
                if "deltabit" in html.lower() and "deltabit" not in current_url.lower():
                     # Maybe we are already on the page but URL didn't update?
                     pass
                break
        return current_url, ua, cookies

    def _build_result(self, video_url: str, referer: str, ua: str, proxy: str = None, cookies: dict = None) -> dict:
        headers = {"Referer": referer, "User-Agent": ua, "Origin": f"https://{urlparse(referer).netloc}"}
        if cookies:
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in cookies.items()])
        return {"destination_url": video_url, "request_headers": headers, "mediaflow_endpoint": self.mediaflow_endpoint, "bypass_warp": self.bypass_warp_active, "selected_proxy": proxy}

    async def close(self):
        if self.session and not self.session.closed: await self.session.close()
