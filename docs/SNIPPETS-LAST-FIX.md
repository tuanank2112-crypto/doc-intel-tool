# Doc Intel — Code vừa sửa (chữ ký / loại VB / bảng / extract)

Generated for review copy.

## Files

| File | Thay đổi chính |
|------|----------------|
| `web/api-bridge.js` | Chữ ký nửa phải; loại VB chỉ HOA; bảng cần `---`; highlight DOM |
| `app/extract.py` | `to_markdown()` PyMuPDF; fallback aligned_lines chặt hơn |

---

## FILE: `web/api-bridge.js`

```javascript
/**
 * Doc Intel ↔ Frontend «Trợ lý họp UBND» (tro-ly-hop-ubnd-v2.html)
 * Không sửa CSS / scroll / tooltip / highlight của frontend.
 * Chỉ nạp dữ liệu thật + gọi API, dùng API render sẵn có của UI.
 */
(function () {
  "use strict";

  const API = "";

  /** @type {Record<string,{job_id:string,analysis:object}>} */
  const LIVE = {};
  /** @type {File[]} */
  let fileBag = [];

  function log() {
    if (typeof console !== "undefined")
      console.info.apply(console, ["[doc-intel]"].concat([].slice.call(arguments)));
  }

  function notify(msg) {
    if (Array.isArray(window.notifs)) {
      window.notifs.unshift({ t: String(msg), time: "Vừa xong" });
      if (typeof window.renderNotif === "function") window.renderNotif();
      var b = document.getElementById("notifBadge");
      if (b) b.classList.remove("hide");
    }
    log(msg);
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ===== Backend → DOC_DATA (đúng schema frontend) ===== */
  function asText(items, key) {
    if (!items || !items.length) return "—";
    return items
      .map(function (x) {
        if (typeof x === "string") return x;
        return (x && (x[key] || x.point || x.decision || x.impact)) || "";
      })
      .filter(Boolean)
      .join(" ");
  }

  function clip(t, n) {
    t = String(t || "");
    return t.length > n ? t.slice(0, n - 1) + "…" : t;
  }

  function mapDocData(a) {
    var s = a.summary || {};
    var ctx = s.context || "—";
    var main = asText(s.main_content, "point");
    var dec = asText(s.decision_points, "decision");
    var imp = asText(s.impact, "impact");
    var pages = a.total_pages || 0;
    // Cite: dùng page từ item đầu nếu backend có, không thì tr.1–N
    function firstPage(items, key) {
      if (!items || !items.length) return null;
      var x = items[0];
      if (x && x.page != null) return x.page;
      if (x && x.pages && x.pages[0] != null) return x.pages[0];
      if (x && x.related_pages && x.related_pages[0] != null) return x.related_pages[0];
      return null;
    }
    function citeFor(page, fallbackLbl) {
      if (page != null) return { cite: "live-p" + page, citeTxt: "tr." + page };
      return {
        cite: "live-p1",
        citeTxt: pages > 1 ? "tr.1–" + pages : fallbackLbl || "tr.1",
      };
    }
    var cCtx = citeFor(null, pages ? "tr.1–" + pages : "tr.1");
    var cMain = citeFor(firstPage(s.main_content), "nội dung chính");
    var cDec = citeFor(firstPage(s.decision_points), "điểm quyết");
    var cImp = citeFor(firstPage(s.impact), "tác động");
    function four(c, m, d, i) {
      return [
        { ic: "map", lbl: "Bối cảnh", txt: c, cite: cCtx.cite, citeTxt: cCtx.citeTxt },
        { ic: "doc", lbl: "Nội dung chính", txt: m, cite: cMain.cite, citeTxt: cMain.citeTxt },
        { ic: "check", lbl: "Điểm cần quyết định", txt: d, cite: cDec.cite, citeTxt: cDec.citeTxt },
        { ic: "warn", lbl: "Tác động cần lưu ý", txt: i, cite: cImp.cite, citeTxt: cImp.citeTxt },
      ];
    }
    var terms = (a.terminology || []).map(function (t) {
      return {
        name: t.term || t.name || "—",
        cat: "law",
        catL: "pháp lý",
        expl: t.explanation || t.expl || "",
        cite: t.page != null ? "live-p" + t.page : "live-p1",
        citeTxt: (t.clause ? t.clause + " · " : "") + (t.page != null ? "tr." + t.page : "văn bản"),
      };
    });
    var items = (a.suggested_questions || []).map(function (q) {
      return {
        q: typeof q === "string" ? q : q.question || "",
        cite: q.related_pages && q.related_pages[0] != null ? "live-p" + q.related_pages[0] : "live-p1",
        citeTxt:
          q.related_pages && q.related_pages.length
            ? "tr." + q.related_pages.join(",")
            : q.purpose || "gợi ý",
      };
    });
    if (!items.length) {
      items = [{ q: "Thẩm quyền ban hành và căn cứ pháp lý của văn bản?", cite: "live-p1", citeTxt: "tr.1" }];
    }
    return {
      summaries: {
        1: four(clip(ctx, 160), clip(main, 140), clip(dec, 140), clip(imp, 120)),
        5: four(clip(ctx, 320), clip(main, 360), clip(dec, 300), clip(imp, 280)),
        0: four(ctx, main, dec, imp),
      },
      terms: terms,
      questions: [
        { grp: "Gợi ý chuẩn bị họp", color: "navy", items: items.slice(0, 4) },
        {
          grp: "Tác động & tuân thủ",
          color: "green",
          items: items.slice(4, 8).length ? items.slice(4, 8) : items.slice(0, 2),
        },
      ],
    };
  }

  /* ===== Nội dung văn bản: đúng class frontend (.term data-def) — tooltip #tip gốc ===== */
  // Fallback thuật ngữ hành chính–pháp lý (khi API trả ít term) để highlight vẫn chạy
  var FALLBACK_TERMS = [
    { name: "ngân sách nhà nước", def: "Toàn bộ các khoản thu, chi của Nhà nước trong một khoảng thời gian nhất định, được cơ quan nhà nước có thẩm quyền quyết định." },
    { name: "dự toán", def: "Kế hoạch thu, chi ngân sách được cấp có thẩm quyền giao hoặc phê duyệt cho kỳ ngân sách." },
    { name: "thẩm định", def: "Xem xét, đánh giá tính hợp pháp, hợp lý, khả thi trước khi ban hành hoặc phê duyệt." },
    { name: "thẩm quyền", def: "Quyền và trách nhiệm do pháp luật quy định cho cơ quan, người có chức vụ." },
    { name: "hiệu lực", def: "Thời điểm và phạm vi văn bản bắt đầu có giá trị pháp lý." },
    { name: "bãi bỏ", def: "Chấm dứt hiệu lực của văn bản hoặc quy định đã ban hành." },
    { name: "sửa đổi, bổ sung", def: "Thay đổi hoặc thêm nội dung của văn bản đang có hiệu lực." },
    { name: "căn cứ", def: "Văn bản, quy định làm cơ sở pháp lý để ban hành văn bản mới." },
    { name: "UBND", def: "Ủy ban nhân dân — cơ quan hành chính nhà nước ở địa phương." },
    { name: "HĐND", def: "Hội đồng nhân dân — cơ quan quyền lực nhà nước ở địa phương." },
    { name: "Nghị định", def: "Văn bản quy phạm pháp luật do Chính phủ ban hành." },
    { name: "Thông tư", def: "Văn bản QPPL do bộ trưởng, thủ trưởng cơ quan ngang bộ ban hành." },
    { name: "Quyết định", def: "Văn bản do cấp có thẩm quyền ban hành để quyết định một vấn đề cụ thể." },
    { name: "thủ tục hành chính", def: "Trình tự, cách thức thực hiện, hồ sơ và yêu cầu, điều kiện do cơ quan nhà nước quy định." },
    { name: "đơn vị sự nghiệp", def: "Tổ chức do Nhà nước thành lập để cung cấp dịch vụ công, không nhằm mục tiêu lợi nhuận." },
    { name: "nguồn tăng thu", def: "Phần thu ngân sách thực tế vượt so với dự toán được giao." },
    { name: "quỹ dự phòng", def: "Khoản ngân sách dành xử lý nhiệm vụ chi đột xuất, cấp bách." },
    { name: "phân cấp", def: "Chuyển một phần thẩm quyền từ cấp trên xuống cấp dưới theo quy định." },
    { name: "công chức", def: "Công dân được tuyển dụng, bổ nhiệm vào ngạch, chức vụ trong cơ quan nhà nước." },
    { name: "viên chức", def: "Người làm việc tại đơn vị sự nghiệp công lập theo vị trí việc làm." },
  ];

  function termList(a) {
    var out = [];
    var seen = {};
    function add(name, def) {
      name = String(name || "").trim();
      if (name.length < 2) return;
      var k = name.toLowerCase();
      if (seen[k]) return;
      seen[k] = 1;
      out.push({ name: name, def: def || "" });
    }
    (a.terminology || []).forEach(function (t) {
      add(t.term || t.name, t.explanation || t.expl || "");
    });
    (a.important_clauses || []).forEach(function (c) {
      add(c.clause, c.summary || c.why_important || "Điều khoản quan trọng trong văn bản");
    });
    var pe = a.preextract || {};
    (pe.dictionary_terms || []).forEach(function (t) {
      add(t.term || t.name, t.explanation || t.expl || "");
    });
    // Chỉ thêm fallback nếu term đó xuất hiện trong corpus (tránh highlight bừa)
    var corpus = "";
    (a.page_index || []).forEach(function (p) {
      corpus += " " + (p.text || "");
    });
    corpus = corpus.toLowerCase();
    FALLBACK_TERMS.forEach(function (t) {
      if (corpus.indexOf(t.name.toLowerCase()) >= 0) add(t.name, t.def);
    });
    out.sort(function (a, b) {
      return b.name.length - a.name.length;
    });
    // 70 trang: giữ nhiều term để highlight giữa tài liệu (trước đây 50 → rớt)
    return out.slice(0, 120);
  }

  /**
   * Escape + xuống dòng — KHÔNG nhúng term bằng regex vào HTML.
   * Term chỉ tô sau khi DOM đã dựng (highlightTermsInDom) để tránh
   * regex khớp trúng nội dung data-def và làm rò `">` ra màn hình.
   */
  function escNL(s) {
    return esc(s).replace(/\n/g, "<br>");
  }

  function mdTable(md) {
    var lines = String(md || "")
      .split("\n")
      .map(function (l) {
        return l.trim();
      })
      .filter(function (l) {
        return l.charAt(0) === "|";
      });
    if (lines.length < 2) return "";
    function split(line) {
      return line
        .replace(/^\|/, "")
        .replace(/\|$/, "")
        .split("|")
        .map(function (c) {
          return c.trim();
        });
    }
    var rows = lines
      .map(split)
      .filter(function (r) {
        return !r.every(function (c) {
          // ≥2 gạch để lọc cả :-- / -- từ OCR
          return /^:?-{2,}:?$/.test(c) || !c;
        });
      });
    if (!rows.length) return "";
    // Bảng không màu — viền đen, nền trắng (thể thức VB)
    var wrap =
      'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;border:1px solid #333;background:#fff"';
    var table =
      'style="border-collapse:collapse;width:100%;font-size:11.5px;line-height:1.35;font-family:\'Times New Roman\',serif"';
    var th =
      'style="border:1px solid #333;padding:5px 8px;background:#fff;color:#000;font-weight:700;text-align:left;font-size:11px"';
    var td0 =
      'style="border:1px solid #333;padding:4px 8px;vertical-align:top;word-break:break-word"';
    var td1 = td0; // bỏ nền xen kẽ
    var h = "<div " + wrap + "><table " + table + "><thead><tr>";
    rows[0].forEach(function (c) {
      h += "<th " + th + ">" + esc(c) + "</th>";
    });
    h += "</tr></thead><tbody>";
    rows.slice(1).forEach(function (r) {
      h += "<tr>";
      r.forEach(function (c) {
        h += "<td " + td0 + ">" + esc(c) + "</td>";
      });
      h += "</tr>";
    });
    h += "</tbody></table></div>";
    return h;
  }

  /**
   * Giải pháp B — layout VBQPPL trung thực:
   * Times New Roman; thể thức căn giữa; Điều/Khoản trái; body justify.
   * Chữ ký nửa phải; chỉ nhận loại VB khi HOA thật; bảng cần dòng ---.
   * Không nhúng term — highlightTermsInDom sau khi chèn DOM.
   */
  function renderStructuredBody(text, anchorId) {
    if (!text || !String(text).trim()) return "";
    var lines = String(text).replace(/\r/g, "").split("\n");
    var out = [];
    if (anchorId) out.push('<span id="' + esc(anchorId) + '"></span>');
    var buf = [];
    var SERIF = "font-family:'Times New Roman',serif";

    function flushPara() {
      if (!buf.length) return;
      var para = buf.join(" ").replace(/\s+/g, " ").trim();
      buf = [];
      if (para)
        out.push(
          '<p style="' + SERIF + ';text-align:justify;margin:6px 0">' + escNL(para) + "</p>"
        );
    }
    function isMdTableLine(ln) {
      return /^\s*\|/.test(ln);
    }
    /** Markdown table thật luôn có dòng |---|---| */
    function hasSeparatorRow(md) {
      return md.some(function (l) {
        var cells = l.replace(/^\||\|$/g, "").split("|");
        return (
          cells.length > 1 &&
          cells.every(function (c) {
            return /^\s*:?-{2,}:?\s*$/.test(c);
          })
        );
      });
    }
    /** Đầu khối chữ ký (không gồm Chánh VP/Giám đốc — hay nằm trong body) */
    function isSigStart(ln) {
      if (/^(TM\.|KT\.|TL\.|TUQ\.)\s/.test(ln)) return true;
      if (/\(Đã\s*ký\)/i.test(ln)) return true;
      if (
        ln.length <= 30 &&
        /^(THỦ TƯỚNG|CHỦ TỊCH|PHÓ CHỦ TỊCH|BỘ TRƯỞNG|THỨ TRƯỞNG)$/i.test(ln)
      )
        return true;
      return false;
    }

    var typeRe =
      /^(NGHỊ ĐỊNH|QUYẾT ĐỊNH|THÔNG TƯ|THÔNG TƯ LIÊN TỊCH|NGHỊ QUYẾT|CHỈ THỊ|LUẬT|BỘ LUẬT|PHÁP LỆNH|CÔNG VĂN|TỜ TRÌNH|BÁO CÁO|ĐỀ ÁN|QUY CHẾ|HƯỚNG DẪN|KẾ HOẠCH|CHƯƠNG TRÌNH)\b/;

    var i = 0;
    while (i < lines.length) {
      var ln = lines[i].trim();
      if (!ln) {
        flushPara();
        i++;
        continue;
      }

      // Bảng markdown — chỉ khi có dòng phân cách; không thì gỡ pipe (Quốc hiệu 2 cột)
      if (isMdTableLine(ln)) {
        flushPara();
        var md = [];
        while (i < lines.length && (isMdTableLine(lines[i]) || !lines[i].trim())) {
          if (isMdTableLine(lines[i])) md.push(lines[i].trim());
          i++;
        }
        if (hasSeparatorRow(md)) {
          out.push(mdTable(md.join("\n")));
        } else {
          md.forEach(function (row) {
            var plain = row
              .replace(/^\||\|$/g, "")
              .split("|")
              .map(function (c) {
                return c.trim();
              })
              .filter(Boolean)
              .join(" ");
            if (plain)
              out.push(
                '<p style="' + SERIF + ';text-align:center;margin:2px 0">' + escNL(plain) + "</p>"
              );
          });
        }
        continue;
      }
      if (/^Trang\s+\d+\s*\/\s*\d+/i.test(ln)) {
        i++;
        continue;
      }
      if (/^\[BẢNG\b/i.test(ln)) {
        flushPara();
        i++;
        continue;
      }

      // Quốc hiệu
      if (/CỘNG\s*HÒA\s*XÃ\s*HỘI\s*CHỦ\s*NGHĨA\s*VIỆT\s*NAM/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-weight:700;text-transform:uppercase;margin:8px 0 2px">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Tiêu ngữ
      if (/Độc\s*lập\s*[-–—]\s*Tự\s*do\s*[-–—]\s*Hạnh\s*phúc/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-weight:700;margin:0 0 14px">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }

      // Khối chữ ký — nửa phải, canh giữa (đặt TRƯỚC nhánh loại VB)
      if (isSigStart(ln)) {
        flushPara();
        var sig = [ln];
        var j = i + 1;
        while (j < lines.length && sig.length < 6) {
          var nx = lines[j].trim();
          if (!nx) {
            j++;
            continue;
          }
          if (nx.length > 60 || isMdTableLine(nx)) break;
          if (/^(Điều|Chương|Mục|PHẦN|\d+\.)\s/i.test(nx)) break;
          sig.push(nx);
          j++;
        }
        i = j;
        out.push(
          '<div style="' +
            SERIF +
            ';text-align:center;font-weight:600;width:48%;margin:18px 0 6px auto">' +
            sig.map(escNL).join("<br>") +
            "</div>"
        );
        continue;
      }

      // Loại VB lớn — chỉ khi dòng VIẾT HOA thật + ngắn (tránh "Quyết định này có…")
      if (ln === ln.toUpperCase() && ln.length <= 40 && typeRe.test(ln)) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin:16px 0 8px">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Cơ quan ban hành / số hiệu
      if (
        (/^(ỦY\s*BAN|BỘ\s|CHÍNH\s*PHỦ|THỦ\s*TƯỚNG|HỘI\s*ĐỒNG)/i.test(ln) && ln.length < 100) ||
        (/^Số\s*[:：]/i.test(ln) && ln.length < 80)
      ) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-weight:600;margin:4px 0">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Địa danh, ngày tháng
      if (/,\s*ngày\s+\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+\d{4}/i.test(ln) && ln.length < 90) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-style:italic;margin:4px 0 12px">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Trích yếu
      if (/^(Về\s+việc|v\/v)\b/i.test(ln)) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:center;font-weight:600;margin:8px 0 16px">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Chương / Mục / Phần
      if (/^(Chương|Mục|PHẦN)\s+/i.test(ln)) {
        flushPara();
        out.push(
          '<h2 style="' +
            SERIF +
            ';text-align:center;font-weight:700;margin:20px 0 10px">' +
            escNL(ln) +
            "</h2>"
        );
        i++;
        continue;
      }
      // Điều
      if (/^Điều\s+\d+/i.test(ln)) {
        flushPara();
        out.push(
          '<h3 style="' + SERIF + ';font-weight:700;margin:16px 0 8px">' + escNL(ln) + "</h3>"
        );
        i++;
        continue;
      }
      // Khoản "1." "2."
      if (/^\d+\.\s+\S/.test(ln) && ln.length < 500) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:justify;margin:6px 0">' +
            escNL(ln) +
            "</p>"
        );
        i++;
        continue;
      }
      // Heading markdown
      if (/^#{1,3}\s+/.test(ln)) {
        flushPara();
        var ht = ln.replace(/^#+\s*/, "");
        out.push(
          '<h3 style="' +
            SERIF +
            ';text-align:center;font-weight:700;text-transform:uppercase;margin:14px 0 8px">' +
            escNL(ht) +
            "</h3>"
        );
        i++;
        continue;
      }

      buf.push(ln);
      i++;
    }
    flushPara();
    return out.join("\n");
  }

  function buildDocHtml(analysis) {
    var pages = analysis.page_index || [];
    var parts = [];
    for (var i = 0; i < pages.length; i++) {
      var p = pages[i];
      var pid = "live-p" + p.page;
      var text = String(p.text || "").trim();

      if (text) {
        parts.push(renderStructuredBody(text, pid));
        if (p.tables && p.tables.length && text.indexOf("|") < 0) {
          p.tables.forEach(function (t) {
            if (t.markdown) parts.push(mdTable(t.markdown));
            else if (t.html) parts.push(String(t.html));
          });
        }
      } else if (p.html) {
        parts.push('<span id="' + pid + '"></span>');
        parts.push(String(p.html));
      } else {
        parts.push('<span id="' + pid + '"></span>');
      }

      // Biểu đồ / ảnh nhúng của trang (born-digital)
      if (p.images && p.images.length) {
        p.images.forEach(function (src) {
          if (!src || String(src).indexOf("data:image") !== 0) return;
          parts.push(
            '<div class="chart" style="text-align:center;margin:10px 0">' +
              '<img loading="lazy" src="' +
              src +
              '" ' +
              'alt="Biểu đồ" style="max-width:100%;height:auto;border:1px solid #e7e3d8;border-radius:6px">' +
              "</div>"
          );
        });
      }
    }
    return parts.join("\n");
  }

  function ensurePane(docId) {
    var el = document.querySelector('.docpane-inner[data-doc="' + docId + '"]');
    if (el) return el;
    var host = document.getElementById("docpane");
    el = document.createElement("div");
    el.className = "docpane-inner doc";
    el.setAttribute("data-doc", docId);
    el.style.display = "none";
    host.appendChild(el);
    return el;
  }

  /** Load pages in batches so UI stays responsive; full content still available. */
  async function loadAllPages(jobId, totalPages) {
    var all = [];
    var batch = 25;
    for (var from = 1; from <= totalPages; from += batch) {
      var to = Math.min(totalPages, from + batch - 1);
      var r = await fetch(
        API +
          "/v1/jobs/" +
          encodeURIComponent(jobId) +
          "/pages?page_from=" +
          from +
          "&page_to=" +
          to
      );
      if (!r.ok) throw new Error("pages_load_failed");
      var j = await r.json();
      (j.pages || []).forEach(function (p) {
        all.push(p);
      });
    }
    return all;
  }

  /**
   * DOM-safe term highlight: walk text nodes only (không regex HTML/attribute).
   * - Ranh giới từ Unicode (?<![\p{L}]) … (?![\p{L}])
   * - Tô MỌI lần xuất hiện trên mọi trang (không cờ done / không 1-lần-toàn-doc)
   * - Bỏ qua span.term / table / script
   */
  function highlightTermsInDom(root, terms) {
    if (!root || !terms || !terms.length) return 0;
    var count = 0;
    // Giới hạn an toàn DOM (tránh treo UI nếu term cực phổ biến)
    var MAX_HITS = 400;

    function termRe(name) {
      var escName = name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      try {
        return new RegExp("(?<![\\p{L}])(" + escName + ")(?![\\p{L}])", "iu");
      } catch (e) {
        return new RegExp(
          "(?:^|[^A-Za-zÀ-ỹĂăÂâÊêÔôƠơƯưĐđ])(" + escName + ")(?=[^A-Za-zÀ-ỹĂăÂâÊêÔôƠơƯưĐđ]|$)",
          "i"
        );
      }
    }

    function collectTextNodes(scope) {
      var out = [];
      var walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT, {
        acceptNode: function (node) {
          if (!node.nodeValue || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
          var p = node.parentElement;
          if (!p) return NodeFilter.FILTER_REJECT;
          if (p.closest("span.term, script, style, table, .chart")) return NodeFilter.FILTER_REJECT;
          return NodeFilter.FILTER_ACCEPT;
        },
      });
      while (walker.nextNode()) out.push(walker.currentNode);
      return out;
    }

    // Sort dài → ngắn để "ngân sách nhà nước" thắng "ngân sách"
    var sorted = terms.slice().sort(function (a, b) {
      return (b.name || "").length - (a.name || "").length;
    });

    sorted.forEach(function (t) {
      if (!t.name || t.name.length < 2 || count >= MAX_HITS) return;
      var re = termRe(t.name);
      // Re-collect mỗi term (DOM đã bị split từ term trước)
      var queue = collectTextNodes(root);
      for (var ni = 0; ni < queue.length && count < MAX_HITS; ni++) {
        var textNode = queue[ni];
        if (!textNode.parentNode) continue;
        // Có thể có nhiều match trong 1 node — bọc lần lượt
        while (textNode && textNode.parentNode && count < MAX_HITS) {
          var val = textNode.nodeValue;
          if (!val) break;
          var m = re.exec(val);
          if (!m) break;
          var matched = m[1] != null ? m[1] : m[0];
          var idx = m[1] != null ? m.index + (m[0].length - matched.length) : m.index;
          if (val.slice(idx, idx + matched.length).toLowerCase() !== matched.toLowerCase()) {
            idx = val.toLowerCase().indexOf(matched.toLowerCase());
            if (idx < 0) break;
          }
          var before = val.slice(0, idx);
          var hit = val.slice(idx, idx + matched.length);
          var after = val.slice(idx + matched.length);
          var span = document.createElement("span");
          span.className = "term";
          span.setAttribute("data-def", t.def || "");
          span.textContent = hit;
          var parent = textNode.parentNode;
          if (before) parent.insertBefore(document.createTextNode(before), textNode);
          parent.insertBefore(span, textNode);
          if (after) {
            var afterNode = document.createTextNode(after);
            parent.insertBefore(afterNode, textNode);
            parent.removeChild(textNode);
            textNode = afterNode; // tiếp tục match còn lại trong phần sau
          } else {
            parent.removeChild(textNode);
            textNode = null;
          }
          count++;
        }
      }
    });
    return count;
  }

  async function applyAnalysis(docId, analysis, displayName) {
    LIVE[docId] = { job_id: analysis.job_id, analysis: analysis };
    window.CURRENT_JOB_ID = analysis.job_id;

    if (typeof DOC_DATA !== "undefined") DOC_DATA[docId] = mapDocData(analysis);
    if (typeof DOCS !== "undefined") {
      var types = (analysis.summary && analysis.summary.document_types) || [];
      DOCS[docId] = {
        name: displayName,
        badge: String(types[0] || "TÀI LIỆU").toUpperCase().slice(0, 16),
        meeting: "Đã tải lên",
      };
    }

    // Lazy-load full pages if upload response omitted page_index
    var total = analysis.total_pages || analysis.pages_available || 0;
    if ((!analysis.page_index || !analysis.page_index.length) && analysis.job_id && total) {
      try {
        notify("Đang nạp " + total + " trang văn bản…");
        analysis.page_index = await loadAllPages(analysis.job_id, total);
        LIVE[docId].analysis = analysis;
      } catch (e) {
        log("pages load error", e);
        notify("Không nạp được đầy đủ trang: " + e.message);
      }
    }

    var pane = ensurePane(docId);
    pane.className = "docpane-inner doc";
    pane.setAttribute("data-doc", docId);
    // Chèn text/HTML sạch trước — chỉ tô term trên DOM (không regex HTML)
    pane.innerHTML = buildDocHtml(analysis);

    var terms = termList(analysis);
    var nTerm = highlightTermsInDom(pane, terms);

    var meta = document.querySelector(".doc-meta b[data-count]");
    if (meta) {
      meta.setAttribute("data-count", String(analysis.total_pages || total || 0));
      meta.textContent = String(analysis.total_pages || total || 0);
    }

    if (typeof switchDoc === "function") switchDoc(docId);
    else {
      if (typeof loadDocData === "function") loadDocData(docId);
      if (typeof renderSummary === "function")
        renderSummary(typeof curTier !== "undefined" ? curTier : 5);
      if (typeof renderTerms === "function" && typeof terms !== "undefined") renderTerms(terms);
      if (typeof renderQuestions === "function") renderQuestions();
    }

    try {
      if (window.ScrollTrigger && ScrollTrigger.refresh) ScrollTrigger.refresh();
    } catch (e) {}

    log("live doc ready", docId, "terms:", nTerm, "pages:", (analysis.page_index || []).length);
  }

  /* ===== Hook upload (File thật) ===== */
  function hookWhenReady() {
    if (typeof window.onPickFiles !== "function" || typeof window.confirmUpload !== "function") {
      setTimeout(hookWhenReady, 50);
      return;
    }

    var _onPick = window.onPickFiles;
    window.onPickFiles = function (files) {
      var list = [];
      Array.prototype.forEach.call(files || [], function (f) {
        var name = f && f.name ? f.name : String(f);
        if (!/\.(pdf|docx)$/i.test(name)) {
          notify("Chỉ hỗ trợ PDF/DOCX: " + name);
          return;
        }
        if (f instanceof File) fileBag.push(f);
        list.push(f);
      });
      // UI gốc: pendingFiles.push(f.name) — nhận File được
      return _onPick(list);
    };

    var _remove = window.removeFile;
    window.removeFile = function (k) {
      fileBag.splice(k, 1);
      return _remove(k);
    };

    var _open = window.openUpload;
    window.openUpload = function () {
      fileBag = [];
      return _open.apply(this, arguments);
    };

    window.confirmUpload = async function () {
      if (typeof selFolder === "undefined" || selFolder === null) return;
      if (!fileBag.length) {
        notify("Hãy chọn tệp PDF/DOCX");
        return;
      }
      var btn = document.getElementById("uploadConfirm");
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Đang xử lý…";
      }
      notify("Đang tải lên & phân tích…");
      try {
        var fd = new FormData();
        fileBag.forEach(function (f) {
          fd.append("files", f);
        });
        var title =
          allMeetings && allMeetings[selFolder]
            ? allMeetings[selFolder].name
            : "Họp UBND";
        fd.append("title", title);

        var res = await fetch(API + "/v1/analyze/upload", { method: "POST", body: fd });
        var data = await res.json();
        if (!res.ok) throw new Error(data.detail || data.error || "analyze failed");

        var nUp = meetings.length;
        var src = selFolder < nUp ? meetings[selFolder] : pastMeetings[selFolder - nUp];
        var docId = "live_" + (data.job_id || String(Date.now()));
        var label =
          (data.files && data.files[0] && data.files[0].filename) || fileBag[0].name;
        var displayName =
          fileBag.length > 1 ? label + " (+" + (fileBag.length - 1) + ")" : label;

        src.docs.push({ t: displayName, docId: docId, on: true });
        src.open = true;

        applyAnalysis(docId, data, displayName);
        if (typeof renderMeetings === "function") renderMeetings();
        if (typeof closeUpload === "function") closeUpload();

        var mi = allMeetings.findIndex(function (x) {
          return x.name === src.name;
        });
        if (mi >= 0) {
          allMeetings[mi].open = true;
          if (typeof setMeetBody === "function") setMeetBody(mi, true, true);
          var el = document.querySelector('.meet[data-i="' + mi + '"]');
          if (el) {
            el.classList.add("on");
            el.scrollIntoView({ behavior: "smooth", block: "nearest" });
          }
        }

        notify(
          "Xong · " +
            (data.elapsed_seconds != null ? data.elapsed_seconds + "s" : "?") +
            " · " +
            (data.within_60s ? "<60s" : "≥60s") +
            " · " +
            displayName
        );
        fileBag = [];
        if (Array.isArray(window.pendingFiles)) window.pendingFiles = [];
      } catch (e) {
        console.error(e);
        notify("Lỗi: " + e.message);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = "Tải lên";
        }
      }
    };

    /* ===== Q&A — cùng DOM/animation frontend, trả lời API ===== */
    window.addQ = function (q) {
      var thread = document.getElementById("thread");
      var ask = document.createElement("div");
      ask.className = "ask";
      ask.textContent = q;
      thread.appendChild(ask);
      var reduce =
        typeof window.reduce !== "undefined"
          ? window.reduce
          : window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (!reduce && window.gsap)
        gsap.from(ask, { y: 12, autoAlpha: 0, duration: 0.4, ease: "back.out(1.6)" });
      if (typeof scrollThread === "function") scrollThread();

      var typing = document.createElement("div");
      typing.className = "ans";
      typing.innerHTML =
        '<div class="ans-body"><div class="typing"><i></i><i></i><i></i></div></div>';
      thread.appendChild(typing);
      if (typeof scrollThread === "function") scrollThread();
      var dotsTween;
      if (!reduce && window.gsap) {
        gsap.from(typing, { y: 8, autoAlpha: 0, duration: 0.3 });
        dotsTween = gsap.to(typing.querySelectorAll(".typing i"), {
          y: -5,
          duration: 0.35,
          repeat: -1,
          yoyo: true,
          stagger: 0.12,
          ease: "sine.inOut",
        });
      }

      var jobId = window.CURRENT_JOB_ID;
      if (typeof curDoc === "string" && LIVE[curDoc]) {
        jobId = LIVE[curDoc].job_id;
        window.CURRENT_JOB_ID = jobId;
      }

      function done(html) {
        if (dotsTween) dotsTween.kill();
        typing.remove();
        var ans = document.createElement("div");
        ans.className = "ans";
        ans.innerHTML = '<div class="ans-body">' + html + "</div>";
        thread.appendChild(ans);
        if (typeof scrollThread === "function") scrollThread();
        if (!reduce && window.gsap) {
          gsap.from(ans, { y: 10, autoAlpha: 0, duration: 0.4 });
          var chips = ans.querySelectorAll(".cites .chip");
          if (chips.length)
            gsap.from(chips, {
              scale: 0.8,
              autoAlpha: 0,
              stagger: 0.1,
              delay: 0.15,
              duration: 0.3,
              ease: "back.out(2)",
            });
          var chk = ans.querySelector(".vchk path");
          if (chk && chk.getTotalLength) {
            var len = chk.getTotalLength();
            gsap.fromTo(
              chk,
              { strokeDasharray: len, strokeDashoffset: len },
              { strokeDashoffset: 0, duration: 0.5, delay: 0.3, ease: "power2.out" }
            );
          }
        }
      }

      if (!jobId) {
        setTimeout(function () {
          done(
            "Tài liệu demo tĩnh chưa gắn máy chủ. Dùng <b>Nhập tài liệu</b> để tải PDF/DOCX và hỏi đáp kèm trích dẫn trang."
          );
        }, 700);
        return;
      }

      fetch(API + "/v1/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: jobId, question: q }),
      })
        .then(function (r) {
          return r.json().then(function (j) {
            return { ok: r.ok, j: j };
          });
        })
        .then(function (x) {
          if (!x.ok) throw new Error(x.j.detail || x.j.error || "ask failed");
          var j = x.j;
          var cites = (j.citations || [])
            .map(function (c) {
              var id = c.page != null ? "live-p" + c.page : "live-p1";
              var label =
                (c.clause ? c.clause + " · " : "") +
                (c.page != null ? "tr." + c.page : "văn bản");
              return (
                '<span class="chip" onclick="goCite(\'' +
                id +
                "')\">📍 " +
                esc(label) +
                "</span>"
              );
            })
            .join("");
          var modeLabel =
            j.answer_mode === "heuristic_search" || j.llm_used === false
              ? "Tìm kiếm thô (chưa LLM)"
              : "AI · đã đối chiếu văn bản gốc";
          done(
            esc(j.answer || "—").replace(/\n/g, "<br>") +
              (cites ? '<div class="cites">' + cites + "</div>" : "") +
              '<div class="verify"><svg class="i vchk" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6 9 17l-5-5"/></svg>' +
              esc(modeLabel) +
              (j.confidence ? " · " + esc(j.confidence) : "") +
              "</div>"
          );
        })
        .catch(function (e) {
          done("Không trả lời được: " + esc(e.message));
        });
    };

    var _switch = window.switchDoc;
    if (typeof _switch === "function") {
      window.switchDoc = function (id) {
        var r = _switch.apply(this, arguments);
        if (LIVE[id]) window.CURRENT_JOB_ID = LIVE[id].job_id;
        return r;
      };
    }

    log("hooks ready — frontend UBND tỉnh + backend Doc Intel");
  }

  fetch(API + "/health")
    .then(function (r) {
      return r.json();
    })
    .then(function (h) {
      var note = document.querySelector(".login-note");
      if (note && h) {
        note.textContent = h.llm_enabled
          ? "UBND tỉnh · máy chủ sẵn sàng · " + (h.model || "AI")
          : "UBND tỉnh · máy chủ online · chưa bật AI key";
      }
    })
    .catch(function () {});

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", hookWhenReady);
  } else {
    hookWhenReady();
  }
})();
```

---

## FILE: `app/extract.py`

```python
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PageText:
    page: int  # 1-based
    text: str
    char_count: int = 0
    html: str = ""  # optional rich HTML for UI (tables preserved)
    tables: list[dict[str, Any]] = field(default_factory=list)
    # data:image/png;base64,... — biểu đồ/hình nhúng (không phải full-page scan)
    images: list[str] = field(default_factory=list)
    ocr_applied: bool = False  # True nếu trang được Gemini Vision lấp text

    def __post_init__(self) -> None:
        self.char_count = len(self.text)


@dataclass
class DocumentText:
    path: str
    filename: str
    doc_id: str
    file_type: str
    pages: list[PageText] = field(default_factory=list)
    engine: str = ""
    total_pages: int = 0
    total_chars: int = 0
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    def full_text(self, page_markers: bool = True) -> str:
        parts: list[str] = []
        for p in self.pages:
            if page_markers:
                parts.append(f"\n----- TRANG {p.page} -----\n{p.text}")
            else:
                parts.append(p.text)
        return "\n".join(parts).strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _clean_page_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _cell_clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).replace("\x00", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _matrix_to_markdown(rows: list[list[str]]) -> str:
    """Render a 2D cell matrix as GitHub-style markdown table."""
    if not rows:
        return ""
    # normalize width
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    # drop fully empty rows/cols
    norm = [r for r in norm if any(c.strip() for c in r)]
    if not norm:
        return ""
    keep_cols = [i for i in range(width) if any(r[i].strip() for r in norm)]
    if not keep_cols:
        return ""
    norm = [[r[i] for i in keep_cols] for r in norm]
    width = len(keep_cols)

    def esc(c: str) -> str:
        return c.replace("|", "\\|").replace("\n", " ")

    header = norm[0]
    # if first row looks like data (all numbers), invent headers
    body_start = 1
    if width and all(re.fullmatch(r"[\d.,%\-\s]+", c or "") for c in header if c):
        header = [f"Cột {i + 1}" for i in range(width)]
        body_start = 0
    lines = [
        "| " + " | ".join(esc(c) for c in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for r in norm[body_start:]:
        lines.append("| " + " | ".join(esc(c) for c in r) + " |")
    return "\n".join(lines)


def _matrix_to_html(rows: list[list[str]], caption: str = "") -> str:
    """Compact, readable table HTML (inline styles — no frontend CSS edits)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    norm = [list(r) + [""] * (width - len(r)) for r in rows]
    norm = [r for r in norm if any(c.strip() for c in r)]
    if not norm:
        return ""
    # drop empty columns
    keep = [i for i in range(width) if any((r[i] or "").strip() for r in norm)]
    if not keep:
        return ""
    norm = [[r[i] for i in keep] for r in norm]

    wrap = (
        'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;'
        'border:1px solid #e7e3d8;border-radius:8px;background:#fff"'
    )
    table = (
        'style="border-collapse:collapse;width:100%;font-size:11.5px;'
        'line-height:1.35;font-family:inherit"'
    )
    th = (
        'style="border:1px solid #d8d3c5;padding:5px 8px;background:#1a1f2b;'
        'color:#ffcd00;font-weight:600;text-align:left;white-space:nowrap;'
        'font-size:11px"'
    )
    td = (
        'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;'
        'color:#1a1f2b;word-break:break-word"'
    )
    td_alt = (
        'style="border:1px solid #ebe6da;padding:4px 8px;vertical-align:top;'
        'color:#1a1f2b;word-break:break-word;background:#fbf8f1"'
    )
    cap = (
        'style="caption-side:top;text-align:left;padding:6px 8px 2px;'
        'font-size:11px;font-weight:600;color:#5b5f66"'
    )

    parts = [f"<div {wrap}><table {table}>"]
    if caption:
        parts.append(f"<caption {cap}>{_html_esc(caption)}</caption>")
    parts.append("<thead><tr>")
    for c in norm[0]:
        parts.append(f"<th {th}>{_html_esc(c)}</th>")
    parts.append("</tr></thead><tbody>")
    for ri, r in enumerate(norm[1:]):
        parts.append("<tr>")
        cell_s = td_alt if ri % 2 else td
        for c in r:
            parts.append(f"<td {cell_s}>{_html_esc(c)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></div>")
    return "".join(parts)


def _html_esc(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bbox_overlap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 < bx0 or bx1 < ax0 or ay1 < by0 or by1 < ay0)


def _extract_tables_pymupdf(page: Any) -> list[dict[str, Any]]:
    """
    Ưu tiên PyMuPDF find_tables + to_markdown() — giữ nguyên ô, tránh cắt giữa từ
    theo toạ độ x (nguyên nhân "CỘ NG", "heo", "t ủa").
    """
    out: list[dict[str, Any]] = []
    try:
        finder = page.find_tables()
    except Exception:
        return out
    tables = getattr(finder, "tables", None) or []
    for i, tab in enumerate(tables):
        matrix: list[list[str]] = []
        md = ""
        # 1) to_markdown trực tiếp từ engine PyMuPDF (ổn định nhất)
        if hasattr(tab, "to_markdown"):
            try:
                md_raw = tab.to_markdown() or ""
                md = str(md_raw).strip()
            except Exception:
                md = ""
        try:
            raw = tab.extract() or []
            matrix = [[_cell_clean(c) for c in (row or [])] for row in raw]
        except Exception:
            matrix = []
        if not matrix and not md:
            continue
        if matrix and sum(1 for r in matrix for c in r if c) < 2:
            continue
        # 2) fallback matrix → markdown nếu to_markdown trống
        if not md and matrix:
            md = _matrix_to_markdown(matrix)
        if not md or "|" not in md:
            continue
        # Bắt buộc có dòng phân cách Markdown (bảng thật)
        has_sep = any(
            all(re.fullmatch(r":?-{2,}:?", (c or "").strip() or "") for c in row)
            for row in [
                [c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in md.splitlines()
                if ln.strip().startswith("|")
            ]
            if len(row) > 1
        )
        # to_markdown thường đã có ---; extract-only thì tự chèn qua _matrix_to_markdown
        if not has_sep and matrix:
            md = _matrix_to_markdown(matrix)
        html = _matrix_to_html(matrix, caption=f"Bảng {i + 1}") if matrix else ""
        if not html and md:
            # dựng html đơn giản từ markdown lines
            rows_md = [
                [c.strip() for c in ln.strip().strip("|").split("|")]
                for ln in md.splitlines()
                if ln.strip().startswith("|")
                and not all(re.fullmatch(r":?-{2,}:?", c.strip() or "") for c in ln.strip().strip("|").split("|"))
            ]
            if rows_md:
                html = _matrix_to_html(rows_md, caption=f"Bảng {i + 1}")
                matrix = rows_md
        bbox = tuple(float(x) for x in (tab.bbox if hasattr(tab, "bbox") else (0, 0, 0, 0)))
        out.append(
            {
                "index": i + 1,
                "bbox": bbox,
                "rows": len(matrix) if matrix else md.count("\n"),
                "cols": max((len(r) for r in matrix), default=0),
                "markdown": md,
                "html": html,
                "matrix": matrix,
                "source": "pymupdf_find_tables",
            }
        )
    return out


def _reconstruct_lines_from_words(page: Any) -> list[dict[str, Any]]:
    """Group words into visual lines (reading order)."""
    try:
        words = page.get_text("words") or []  # x0,y0,x1,y1,"word",block,line,wno
    except Exception:
        return []
    if not words:
        return []
    # sort by y then x
    words = sorted(words, key=lambda w: (round(w[1], 1), w[0]))
    lines: list[dict[str, Any]] = []
    cur: list[Any] = []
    cur_y: float | None = None
    y_tol = 3.5

    def flush() -> None:
        nonlocal cur, cur_y
        if not cur:
            return
        cur.sort(key=lambda w: w[0])
        text = " ".join(w[4] for w in cur)
        x0 = min(w[0] for w in cur)
        y0 = min(w[1] for w in cur)
        x1 = max(w[2] for w in cur)
        y1 = max(w[3] for w in cur)
        # gaps between words → possible column separators
        gaps = []
        for a, b in zip(cur, cur[1:]):
            gap = b[0] - a[2]
            if gap > 8:
                gaps.append((gap, a[2], b[0], a[4], b[4]))
        lines.append(
            {
                "text": text,
                "bbox": (x0, y0, x1, y1),
                "words": cur[:],
                "gaps": gaps,
            }
        )
        cur = []
        cur_y = None

    for w in words:
        y = w[1]
        if cur_y is None or abs(y - cur_y) <= y_tol:
            cur.append(w)
            cur_y = y if cur_y is None else (cur_y * 0.7 + y * 0.3)
        else:
            flush()
            cur = [w]
            cur_y = y
    flush()
    return lines


def _detect_aligned_table_from_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Fallback khi find_tables miss — chỉ nhận cụm ≥3 hàng, gap ngang lớn (≥28pt)
    để tránh cắt giữa từ (CỘ NG / NGH ĨA) và nhầm header 2 cột thể thức.
    """
    if len(lines) < 3:
        return []

    def line_cols(line: dict[str, Any]) -> list[str]:
        words = line["words"]
        if len(words) < 2:
            return [line["text"]]
        # Gap lớn hơn khoảng cách giữa chữ trong từ VN (tránh cắt giữa glyph)
        cols: list[str] = []
        buf = [words[0][4]]
        for a, b in zip(words, words[1:]):
            gap = b[0] - a[2]
            if gap > 28:
                cols.append(" ".join(buf))
                buf = [b[4]]
            else:
                buf.append(b[4])
        cols.append(" ".join(buf))
        return cols

    multi = []
    for i, ln in enumerate(lines):
        cols = line_cols(ln)
        if len(cols) >= 2:
            multi.append((i, cols, ln))

    if len(multi) < 3:
        return []

    tables: list[dict[str, Any]] = []
    cluster: list[tuple[int, list[str], dict]] = [multi[0]]
    for item in multi[1:]:
        prev_i = cluster[-1][0]
        if item[0] <= prev_i + 2:
            cluster.append(item)
        else:
            if len(cluster) >= 3:
                t = _cluster_to_table(cluster)
                if t:
                    tables.append(t)
            cluster = [item]
    if len(cluster) >= 3:
        t = _cluster_to_table(cluster)
        if t:
            tables.append(t)
    return tables


def _cluster_to_table(cluster: list[tuple[int, list[str], dict]]) -> dict[str, Any] | None:
    matrices = [c[1] for c in cluster]
    width = max(len(r) for r in matrices)
    if width < 2 or len(matrices) < 3:
        return None
    matrix = [r + [""] * (width - len(r)) for r in matrices]
    filled = sum(1 for r in matrix for c in r if c.strip())
    if filled < width * 2:
        return None
    # Bỏ cụm 2 cột ngắn ở đầu trang (thể thức: cơ quan | Quốc hiệu)
    y0 = min(c[2]["bbox"][1] for c in cluster)
    avg_cell = filled and (
        sum(len(c) for r in matrix for c in r if c.strip()) / max(1, filled)
    )
    if width == 2 and len(matrix) <= 4 and y0 < 120 and avg_cell < 28:
        return None
    # Bỏ nếu nhiều ô 1–2 ký tự lẻ (dấu hiệu cắt giữa từ)
    tiny = sum(1 for r in matrix for c in r if c.strip() and len(c.strip()) <= 2)
    if tiny >= max(3, filled // 4):
        return None
    md = _matrix_to_markdown(matrix)
    html = _matrix_to_html(matrix)
    y1 = max(c[2]["bbox"][3] for c in cluster)
    x0 = min(c[2]["bbox"][0] for c in cluster)
    x1 = max(c[2]["bbox"][2] for c in cluster)
    return {
        "index": 0,
        "bbox": (x0, y0, x1, y1),
        "rows": len(matrix),
        "cols": width,
        "markdown": md,
        "html": html,
        "matrix": matrix,
        "source": "aligned_lines",
    }


def _page_text_excluding_tables(page: Any, table_bboxes: list[tuple[float, float, float, float]]) -> str:
    """Extract body text while skipping regions covered by detected tables."""
    try:
        blocks = page.get_text("blocks") or []
    except Exception:
        return _clean_page_text(page.get_text("text") or "")

    parts: list[str] = []
    for b in blocks:
        # block: x0,y0,x1,y1,text,block_no,block_type
        if len(b) < 5:
            continue
        x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
        if not str(text).strip():
            continue
        bb = (float(x0), float(y0), float(x1), float(y1))
        # skip if heavily overlapping a table
        skip = False
        for tb in table_bboxes:
            if _bbox_overlap(bb, tb):
                # if block mostly inside table bbox, skip
                skip = True
                break
        if skip:
            continue
        parts.append(str(text).strip())
    if not parts:
        return _clean_page_text(page.get_text("text") or "")
    return _clean_page_text("\n".join(parts))


def extract_page_images(
    pdf_path: str | Path,
    page_index: int,
    *,
    min_w: int = 90,
    min_h: int = 90,
    full_page_ratio: float = 0.82,
) -> list[str]:
    """
    Trích ảnh raster nhúng trên 1 trang (0-based) → data-URL PNG.
    Bỏ icon/con dấu nhỏ và ảnh gần full-page (trang scan).
    Không bắt biểu đồ vector — cần clip pixmap nếu tài liệu vẽ bằng path.
    """
    import base64

    import fitz

    doc = fitz.open(pdf_path)
    try:
        page = doc.load_page(page_index)
        page_area = float(page.rect.width * page.rect.height) or 1.0
        out: list[str] = []
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                pix = fitz.Pixmap(doc, xref)
            except Exception:
                continue
            try:
                if pix.width < min_w or pix.height < min_h:
                    continue
                if (pix.width * pix.height) > full_page_ratio * page_area:
                    continue  # full-page scan / background
                if pix.n - pix.alpha >= 4:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                png = pix.tobytes("png")
                out.append("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
            finally:
                pix = None  # type: ignore[assignment]
        return out
    finally:
        doc.close()


def _extract_pdf_page(page: Any, page_no: int) -> PageText:
    """One PDF page: preserve tables as markdown + HTML."""
    tables = _extract_tables_pymupdf(page)
    if not tables:
        # fallback aligned-word heuristic
        lines = _reconstruct_lines_from_words(page)
        tables = _detect_aligned_table_from_lines(lines)
        for i, t in enumerate(tables):
            t["index"] = i + 1

    bboxes = [tuple(t["bbox"]) for t in tables if t.get("bbox")]
    body = _page_text_excluding_tables(page, bboxes) if tables else _clean_page_text(page.get_text("text") or "")

    # Compose plain text for LLM / search (markdown tables kept)
    text_parts: list[str] = []
    if body:
        text_parts.append(body)
    for t in tables:
        text_parts.append(f"\n[BẢNG {t.get('index', '')} — {t.get('rows')}×{t.get('cols')}]\n{t.get('markdown', '')}\n")

    # Compose HTML for UI
    html_parts: list[str] = []
    if body:
        # paragraphs
        for para in re.split(r"\n{2,}", body):
            p = para.strip()
            if not p:
                continue
            html_parts.append(f"<p>{_html_esc(p).replace(chr(10), '<br>')}</p>")
    for t in tables:
        html_parts.append(t.get("html") or "")

    return PageText(
        page=page_no,
        text="\n".join(text_parts).strip(),
        html="\n".join(html_parts),
        tables=[
            {
                "index": t.get("index"),
                "rows": t.get("rows"),
                "cols": t.get("cols"),
                "markdown": t.get("markdown"),
                "html": t.get("html"),
                "source": t.get("source", "find_tables"),
            }
            for t in tables
        ],
    )


def _extract_pdf(path: Path) -> tuple[list[PageText], str, list[str]]:
    warnings: list[str] = []
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise RuntimeError("PyMuPDF (pymupdf) is required for PDF extraction") from e

    doc = fitz.open(path)
    pages: list[PageText] = []
    try:
        n = doc.page_count

        def _one(i: int) -> PageText:
            page = doc.load_page(i)
            return _extract_pdf_page(page, i + 1)

        # Table detection is not always thread-safe across all builds — serialize for safety
        # but keep modest parallel for large docs when no tables needed is hard to know;
        # use sequential for correctness of find_tables.
        workers = 1 if n <= 2 else min(4, n)
        if workers == 1:
            pages = [_one(i) for i in range(n)]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_one, i): i for i in range(n)}
                tmp: dict[int, PageText] = {}
                for fut in as_completed(futs):
                    i = futs[fut]
                    tmp[i] = fut.result()
                pages = [tmp[i] for i in range(n)]

        empty = sum(1 for p in pages if not p.text)
        n_tables = sum(len(p.tables) for p in pages)
        if empty and empty == n:
            warnings.append("PDF có vẻ scan/ảnh — text layer trống; sẽ thử Gemini Vision OCR nếu bật.")
        elif empty:
            warnings.append(f"{empty}/{n} trang không có text (sẽ OCR Vision nếu bật).")
        if n_tables:
            warnings.append(f"Đã giữ cấu trúc {n_tables} bảng (markdown/HTML).")
        else:
            warnings.append(
                "Không phát hiện bảng có đường kẻ; đã thử căn cột theo vị trí chữ."
            )

        # --- Gemini Vision OCR (PDF → image → text), parallel ---
        engine = "pymupdf+tables"
        try:
            from .vision_ocr import enrich_pages_with_vision, ocr_enabled
            from .config import settings
            import asyncio

            if ocr_enabled():
                mode = (settings.ocr_mode or "auto").lower()
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # nested: run in new loop thread
                        import concurrent.futures

                        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                            pages, ocr_warn = pool.submit(
                                lambda: asyncio.run(
                                    enrich_pages_with_vision(path, pages, mode=mode)
                                )
                            ).result()
                    else:
                        pages, ocr_warn = loop.run_until_complete(
                            enrich_pages_with_vision(path, pages, mode=mode)
                        )
                except RuntimeError:
                    pages, ocr_warn = asyncio.run(
                        enrich_pages_with_vision(path, pages, mode=mode)
                    )
                warnings.extend(ocr_warn)
                if any("OCR Gemini" in w for w in ocr_warn):
                    engine = "pymupdf+tables+gemini-vision"
            elif empty:
                warnings.append(
                    "Bật OCR: set GEMINI_API_KEY + OCR_MODE=auto|always (gemini-2.0-flash Vision)."
                )
        except Exception as e:
            warnings.append(f"OCR Vision lỗi: {str(e)[:160]}")

        # --- Biểu đồ / hình nhúng (chỉ trang born-digital, không OCR) ---
        n_imgs = 0
        try:
            for i, pt in enumerate(pages):
                if getattr(pt, "ocr_applied", False):
                    pt.images = []
                    continue
                # Trang gần như không text → khả năng scan; skip để tránh dán full-page
                if page_needs_ocr_local(pt.text):
                    pt.images = []
                    continue
                imgs = extract_page_images(path, i)
                pt.images = imgs
                n_imgs += len(imgs)
            if n_imgs:
                warnings.append(f"Đã trích {n_imgs} ảnh/biểu đồ nhúng (raster).")
        except Exception as e:
            warnings.append(f"Trích ảnh trang lỗi: {str(e)[:120]}")

        return pages, engine, warnings
    finally:
        doc.close()


def page_needs_ocr_local(text: str, *, min_chars: int = 40) -> bool:
    """Heuristic local (tránh import cycle với vision_ocr lúc load)."""
    t = (text or "").strip()
    if len(t) < min_chars:
        return True
    bad = sum(1 for c in t if ord(c) < 9 or c == "\ufffd")
    if bad > max(5, len(t) * 0.05):
        return True
    return False


def _extract_docx(path: Path) -> tuple[list[PageText], str, list[str]]:
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph
        from docx.table import Table
    except ImportError as e:
        raise RuntimeError("python-docx is required for Word extraction") from e

    document = Document(str(path))
    blocks: list[str] = []
    html_blocks: list[str] = []
    all_tables: list[dict[str, Any]] = []
    table_i = 0

    # Iterate body in order (paragraphs + tables)
    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, document)
            t = (p.text or "").strip()
            if t:
                blocks.append(t)
                html_blocks.append(f"<p>{_html_esc(t)}</p>")
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            matrix: list[list[str]] = []
            for row in table.rows:
                # dedupe merged cell repeats in python-docx
                cells: list[str] = []
                seen_ids: set[int] = set()
                for cell in row.cells:
                    cid = id(cell._tc)
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    cells.append(_cell_clean(cell.text))
                matrix.append(cells)
            if matrix and any(any(c for c in r) for r in matrix):
                table_i += 1
                md = _matrix_to_markdown(matrix)
                html = _matrix_to_html(matrix, caption=f"Bảng {table_i}")
                blocks.append(f"\n[BẢNG {table_i} — {len(matrix)}×{max(len(r) for r in matrix)}]\n{md}\n")
                html_blocks.append(html)
                all_tables.append(
                    {
                        "index": table_i,
                        "rows": len(matrix),
                        "cols": max(len(r) for r in matrix),
                        "markdown": md,
                        "html": html,
                        "source": "docx",
                    }
                )

    full = "\n".join(blocks).strip()
    full_html = "\n".join(html_blocks)
    page_size = 2200
    pages: list[PageText] = []
    if not full:
        pages = [PageText(page=1, text="", html="", tables=[])]
    else:
        # Keep tables intact: split by paragraphs but don't cut mid-table
        chunks: list[tuple[str, str]] = []
        buf_t, buf_h = "", ""
        # simple split: use text blocks already joined — page by char with table boundaries
        parts = re.split(r"(\n\[BẢNG \d+[^\]]*\]\n(?:\|.+\n)+)", full)
        html_parts = re.split(r'(<table class="doc-table">.*?</table>)', full_html, flags=re.S)
        # Paging: associate tables với đúng page chứa markdown của bảng đó
        # (không đổ tất cả vào page 0 gây citation sai)
        for i in range(0, len(full), page_size):
            chunk = full[i : i + page_size]
            # Bảng xuất hiện trong chunk này (khớp 40 ký tự đầu markdown)
            t_in = [t for t in all_tables if t.get("markdown") and t["markdown"][:40] in chunk]
            # HTML: chỉ include đoạn html tương ứng với chunk (approximate)
            # Để đơn giản: attach full_html vào page đầu, page sau để trống
            h_slice = full_html if i == 0 else ""
            pages.append(
                PageText(
                    page=len(pages) + 1,
                    text=chunk,
                    html=h_slice,
                    tables=t_in,  # bảng gắn đúng page chứa nó
                )
            )
        # Thêm metadata tổng hợp vào page 1: chỉ ghi tables chưa xuất hiện ở page nào
        if pages:
            assigned = {t["index"] for page in pages for t in page.tables}
            unassigned = [t for t in all_tables if t["index"] not in assigned]
            if unassigned:
                # Các bảng không khớp chunk nào → gán vào page đầu
                pages[0].tables = pages[0].tables + unassigned
            if not pages[0].html:
                pages[0].html = full_html

    return (
        pages,
        "python-docx+tables",
        [
            "Word: bảng được giữ dạng markdown/HTML.",
            "Số trang Word là ước lượng theo độ dài.",
        ],
    )


def extract_document(path: str | Path) -> DocumentText:
    path = Path(path).resolve()
    if not path.exists():
        raise FileNotFoundError(str(path))

    suffix = path.suffix.lower()
    sha = _sha256_file(path)
    doc_id = sha[:16]

    if suffix == ".pdf":
        pages, engine, warnings = _extract_pdf(path)
        file_type = "pdf"
    elif suffix in (".docx", ".doc"):
        if suffix == ".doc":
            raise ValueError("Định dạng .doc cũ không hỗ trợ — chuyển sang .docx hoặc PDF")
        pages, engine, warnings = _extract_docx(path)
        file_type = "docx"
    else:
        raise ValueError(f"Định dạng không hỗ trợ: {suffix}. Dùng PDF hoặc DOCX.")

    total_chars = sum(p.char_count for p in pages)
    return DocumentText(
        path=str(path),
        filename=path.name,
        doc_id=doc_id,
        file_type=file_type,
        pages=pages,
        engine=engine,
        total_pages=len(pages),
        total_chars=total_chars,
        sha256=sha,
        warnings=warnings,
    )


def extract_folder(
    folder: str | Path,
    *,
    patterns: tuple[str, ...] = ("*.pdf", "*.docx", "*.PDF", "*.DOCX"),
    max_pages: int | None = None,
) -> list[DocumentText]:
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))

    files: list[Path] = []
    seen: set[Path] = set()
    for pat in patterns:
        for p in sorted(folder.glob(pat)):
            if p.is_file() and p not in seen:
                seen.add(p)
                files.append(p)

    docs: list[DocumentText] = []
    page_budget = max_pages
    for f in files:
        doc = extract_document(f)
        if page_budget is not None:
            if page_budget <= 0:
                break
            if doc.total_pages > page_budget:
                doc.pages = doc.pages[:page_budget]
                doc.total_pages = len(doc.pages)
                doc.total_chars = sum(p.char_count for p in doc.pages)
                doc.warnings.append(f"Cắt còn {page_budget} trang theo budget.")
                page_budget = 0
            else:
                page_budget -= doc.total_pages
        docs.append(doc)
    return docs


def chunk_pages(
    pages: list[PageText],
    *,
    pages_per_chunk: int = 8,  # legacy; ignored — chunk by char budget
    max_chars: int = 8000,
    source_file: str = "",
    max_chunks: int | None = None,
    hard_max_chunks: int | None = None,
) -> list[dict[str, Any]]:
    """
    Chia trang thành chunk map-reduce theo ngưỡng ký tự.

    - KHÔNG _smart_truncate / không cắt giữa: mỗi trang vào đúng một chunk
      (trừ khi gộp theo hard_max để chặn chi phí — vẫn giữ đủ text, không bỏ giữa).
    - max_chunks / hard_max_chunks: trần số chunk (mặc định 40).
    """
    import math

    n = len(pages)
    if n == 0:
        return []

    cap = hard_max_chunks if hard_max_chunks is not None else max_chunks
    if cap is None or cap <= 0:
        cap = 40

    # 1) Gói trang theo max_chars (không cắt nội dung trang)
    batches: list[list[PageText]] = []
    cur: list[PageText] = []
    cur_len = 0
    for p in pages:
        t = getattr(p, "text", "") or ""
        tlen = len(t)
        if cur and cur_len + tlen > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += tlen
    if cur:
        batches.append(cur)

    # 2) Tài liệu cực dài → gộp đều batch, vẫn KHÔNG cắt giữa
    if len(batches) > cap:
        step = math.ceil(len(batches) / cap)
        merged: list[list[PageText]] = []
        for i in range(0, len(batches), step):
            group: list[PageText] = []
            for b in batches[i : i + step]:
                group.extend(b)
            if group:
                merged.append(group)
        batches = merged

    chunks: list[dict[str, Any]] = []
    for batch in batches:
        body = "\n".join(f"[Trang {p.page}]\n{p.text or ''}" for p in batch)
        chunks.append(
            {
                "chunk_id": f"{source_file}:{batch[0].page}-{batch[-1].page}",
                "source_file": source_file,
                "page_start": batch[0].page,
                "page_end": batch[-1].page,
                "text": body,
                "char_count": len(body),
            }
        )
    return chunks
```

Generated: 2026-07-18T16:41:42+07:00
