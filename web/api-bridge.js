/**
 * Doc Intel <-> Frontend «Trợ lý họp UBND» (tro-ly-hop-ubnd-v2.html)
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

  /* ===== Backend -> DOC_DATA (đúng schema frontend) ===== */
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
    return out.slice(0, 120);
  }

  /**
   * Escape + xuống dòng — KHÔNG nhúng term bằng regex vào HTML.
   */
  function escNL(s) {
    return esc(s).replace(/\n/g, "<br>");
  }

  function mdTable(md) {
    // to_markdown() của PyMuPDF đặt "<br>" cho xuống dòng trong ô;
    // giữ xuống dòng đúng nghĩa thay vì hiện chữ "<br>".
    function cellHtml(c) {
      return esc(c).replace(/&lt;br\s*\/?&gt;/gi, "<br>");
    }
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
          return /^:?-{2,}:?$/.test(c) || !c;
        });
      });
    if (!rows.length) return "";
    var wrap =
      'style="max-width:100%;overflow-x:auto;margin:8px 0 12px;border:1px solid #333;background:#fff"';
    var table =
      'style="border-collapse:collapse;width:100%;font-size:11.5px;line-height:1.35;font-family:\'Times New Roman\',serif"';
    var th =
      'style="border:1px solid #333;padding:5px 8px;background:#fff;color:#000;font-weight:700;text-align:left;font-size:11px"';
    var td0 =
      'style="border:1px solid #333;padding:4px 8px;vertical-align:top;word-break:break-word"';
    var h = "<div " + wrap + "><table " + table + "><thead><tr>";
    rows[0].forEach(function (c) {
      h += "<th " + th + ">" + cellHtml(c) + "</th>";
    });
    h += "</tr></thead><tbody>";
    rows.slice(1).forEach(function (r) {
      h += "<tr>";
      r.forEach(function (c) {
        h += "<td " + td0 + ">" + cellHtml(c) + "</td>";
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
   * Cơ quan ban hành + số hiệu canh TRÁI (khối trên-trái của VB).
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
        /^(THỦ TƯỚNG|CHỦ TỊCH|PHÓ CHỦ TỊCH|BỘ TRƯỞ NG|THỨ TRƯỞ NG)$/i.test(ln)
      )
        return true;
      return false;
    }

    var typeRe =
      /^(NGHỊ ĐỊ NH|QUYẾT ĐỊ NH|THÔNG TƯ|THÔNG TƯ LIÊN TỊ CH|NGHỊ QUYẾT|CHỈ THỊ|LUẬT|BỘ LUẬT|PHÁP LỆNH|CÔNG VĂN|TỜ TRÌNH|BÁO CÁO|ĐỀ ÁN|QUY CHẾ|HƯỚNG DẪN|KẾ HOẠCH|CHƯƠNG TRÌNH)\b/;

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
      // Cơ quan ban hành / số hiệu — canh TRÁI (khối trên-trái của VB, KHÔNG nhảy ra giữa)
      if (
        (/^(ỦY\s*BAN|BỘ\s|CHÍNH\s*PHỦ|THỦ\s*TƯỚNG|HỘI\s*ĐỒNG)/i.test(ln) && ln.length < 100) ||
        (/^Số\s*[:：]/i.test(ln) && ln.length < 80)
      ) {
        flushPara();
        out.push(
          '<p style="' +
            SERIF +
            ';text-align:left;font-weight:600;margin:4px 0">' +
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
   */
  function highlightTermsInDom(root, terms) {
    if (!root || !terms || !terms.length) return 0;
    var count = 0;
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

    var sorted = terms.slice().sort(function (a, b) {
      return (b.name || "").length - (a.name || "").length;
    });

    sorted.forEach(function (t) {
      if (!t.name || t.name.length < 2 || count >= MAX_HITS) return;
      var re = termRe(t.name);
      var queue = collectTextNodes(root);
      for (var ni = 0; ni < queue.length && count < MAX_HITS; ni++) {
        var textNode = queue[ni];
        if (!textNode.parentNode) continue;
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
            textNode = afterNode;
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
