# Books v1 source coverage

`books-v1` is an initial, bounded set of independently written factual notes
for 20 distinct works present in the fixed 200-record public catalog. Every
catalog record has a mapping status. No copyrighted book text, publisher
marketing copy, review text, benchmark question, or benchmark answer is stored.

## Coverage counts

| Status | Count | Meaning in this version |
| --- | ---: | --- |
| `exact_edition` | 0 | No catalog edition had enough primary evidence to assign a unique edition identity conservatively. |
| `exact_work` | 20 | A reviewed author, publisher, or library source establishes the work, while the catalog edition remains unresolved. |
| `ambiguous` | 17 | The catalog offer combines multiple books or named works, so one work/edition identity would misrepresent it. |
| `unmatched` | 163 | No authoritative source was reviewed in this bounded initial curation, including one non-book retail record. |
| **Total** | **200** | Exactly one status per catalog product ID. |

The 17 structurally ambiguous product IDs are `1`, `10`, `26`, `27`, `33`,
`65`, `70`, `85`, `110`, `124`, `148`, `160`, `164`, `169`, `174`, `176`,
and `184`. Product `58` is explicitly unmatched because it is a milk
multipack rather than a bibliographic work.

## Supported works and evidence

All rows below are work-scoped. Edition details observed during research are
not included in retrievable notes and do not flow to catalog products.

| Product | Work | Source authority | Traceable source | Edition limitation |
| ---: | --- | --- | --- | --- |
| 38 | *Little Women*, Louisa May Alcott | Penguin Random House | https://www.penguinrandomhouse.com/books/292282/little-women-by-louisa-may-alcott-edited-with-an-introduction-by-anne-boyd-rioux-foreword-by-patti-smith/9780143106654/ | The source is an English Penguin Classics edition; the 700-page Vietnamese edition is unresolved. |
| 51 | *Start-up Nation*, Dan Senor and Saul Singer | Hachette Book Group | https://www.hachettebookgroup.com/titles/dan-senor/start-up-nation/9780446541466/?lens=twelve | The catalog omits Saul Singer and describes a 508-page Vietnamese edition. |
| 52 | *Becoming*, Michelle Obama | Penguin Random House | https://www.penguinrandomhouse.com/books/562881/becoming-by-michelle-obama/ | The Vietnamese hardcover edition is unresolved. |
| 59 | *The Social Contract*, Jean-Jacques Rousseau | Project Gutenberg catalog | https://www.gutenberg.org/ebooks/46333 | Only bibliographic title/author/translator/subject fields were used; the auto-generated page summary and full text were not used. |
| 60 | *The Prince*, Niccolò Machiavelli | Project Gutenberg catalog | https://www.gutenberg.org/ebooks/1232 | Only bibliographic title/author/uniform-title/translator/subject fields were used; the auto-generated page summary and full text were not used. |
| 68 | *Einstein: His Life and Universe*, Walter Isaacson | Simon & Schuster | https://www.simonandschuster.com/books/Einstein/Walter-Isaacson/9780743264747 | The publisher's English edition is 704 pages; the catalog's 720-page Vietnamese edition is unresolved. |
| 71 | *The Devotion of Suspect X*, Keigo Higashino | Macmillan | https://us.macmillan.com/books/9781429992312/thedevotionofsuspectx/ | The source establishes the English work and series identity; the Vietnamese edition is unresolved. |
| 76 | *The 7 Habits of Highly Effective People*, Stephen R. Covey | Simon & Schuster | https://www.simonandschuster.com/books/The-7-Habits-of-Highly-Effective-People/Stephen-R-Covey/The-Covey-Habits-Series/9781982137137 | The source is a 2020 anniversary edition; the 536-page Vietnamese edition is unresolved. |
| 78 | *The Phoenix Project*, Gene Kim, Kevin Behr, and George Spafford | IT Revolution | https://itrevolution.com/product/the-phoenix-project/ | The catalog names only Gene Kim and describes a 544-page Vietnamese edition. |
| 80 | *Sapiens: A Brief History of Humankind*, Yuval Noah Harari | Official author site | https://www.ynharari.com/book/sapiens/ | The author page supports the work and high-level subject, not the catalog's 600-page claim. |
| 96 | *The Socrates Express*, Eric Weiner | Simon & Schuster | https://www.simonandschuster.com/books/The-Socrates-Express/Eric-Weiner/9781501129018 | The source is a 352-page English edition; the catalog's 436-page Vietnamese edition is unresolved. |
| 99 | *Show Your Work!*, Austin Kleon | Alpha Books | https://www.alphabooks.vn/nghe-thuat-pr-ban-than | The page connects the Vietnamese and English titles but exposes no unique printing identifier or ISBN, so the mapping remains work-level. |
| 101 | *Flour Water Salt Yeast*, Ken Forkish | Penguin Random House | https://www.penguinrandomhouse.com/books/216098/flour-water-salt-yeast-by-ken-forkish/ | The source is a 272-page English edition; the catalog's 412-page Vietnamese edition is unresolved. |
| 108 | *Morisaki Shoten no Hibi*, Yagisawa Satoshi | National Library of Vietnam, PDF page 177 (viewer index 176), entry 2217 | https://nlv.gov.vn/dmdocuments/tmqg-02-2024.pdf#page=177 | The bibliography reports 177 pages while the catalog reports 180, so edition identity is unresolved. |
| 137 | *The Boy in the Striped Pajamas*, John Boyne | Penguin Random House | https://www.penguinrandomhouse.com/books/665424/el-nino-con-el-pijama-de-rayas-the-boy-in-the-striped-pajamas-by-john-boyne/ | The source is a Spanish edition; the Vietnamese edition is unresolved. |
| 141 | *Economix*, Michael Goodwin, illustrated by Dan E. Burr | Abrams | https://www.abramsbooks.com/product/economix_9780810988392/ | The source is a 304-page English edition; the catalog's 310-page Vietnamese edition is unresolved. |
| 158 | *Steve Jobs*, Walter Isaacson | Simon & Schuster | https://www.simonandschuster.com/books/Steve-Jobs/Walter-Isaacson/9781451648539 | The source is a 656-page English edition; the catalog's 758-page Vietnamese edition is unresolved. |
| 168 | *Dế Mèn phiêu lưu ký*, Tô Hoài | Nhà xuất bản Kim Đồng | https://nxbkimdong.com.vn/products/de-men-phieu-luu-ky-2 | The page identifies an illustrated edition, but does not confirm the catalog's “100 năm Tô Hoài” qualifier; the mapping therefore remains work-level. |
| 179 | *Zero to One*, Peter Thiel and Blake Masters | Penguin Random House | https://www.penguinrandomhouse.com/books/234730/zero-to-one-by-peter-thiel-with-blake-masters/ | The catalog omits Blake Masters and describes a 276-page Vietnamese edition. |
| 190 | *Fahrenheit 451*, Ray Bradbury | Simon & Schuster | https://www.simonandschuster.com/books/Fahrenheit-451/Ray-Bradbury/9781451673265 | The source is a 176-page English edition; the catalog's 232-page Vietnamese edition is unresolved. |

## Access and versioning

Every source note has `document_version` `2026-09-09` and the actual curation
access timestamp `2026-09-09T06:47:25.633Z`. Content hashes are lowercase
SHA-256 over the contract's canonical UTF-8 Markdown form: NFC Unicode, LF line
endings, no trailing line whitespace, at most two consecutive blank lines, and
exactly one final LF.

The initial access policy allows authenticated principals in tenant `default`.
Shopper-mode reads require `ecommerce.read`; merchant-mode reads require both
`ecommerce.read` and `merchant.read`. No principal allowlist narrows those
authenticated users.

## Unsupported details and operational limits

- No mapped row may infer a Vietnamese ISBN, translator, printing, publication
  year, page count, cover, price, or availability from a foreign edition.
- Product 99 lacks a primary unique printing identifier; product 168 lacks
  confirmation that the source page is the catalog's named 100th-anniversary
  edition; product 108 has a 177-versus-180 page-count discrepancy.
- Start-up Nation, The Phoenix Project, and Zero to One have creator omissions
  in the catalog. Their notes preserve the complete authorship shown by the
  reviewed publisher sources.
- The IT Revolution and Macmillan pages were observed with full work metadata
  during research, but automated direct re-fetches were intermittently rejected.
  Their stored notes remain independently written and the URLs remain the
  traceability anchors; a future refresh should re-check availability.
- Project Gutenberg labels apply to the particular English artifacts described
  by its catalog in the USA. No public-domain or reuse claim is made for any
  Vietnamese translation or other edition.
- The remaining 180 catalog records are deliberately not supported by retrievable
  notes in `books-v1`. They require a later source review before publication.
