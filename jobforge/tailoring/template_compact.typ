#set page(
  paper: "us-letter",
  margin: (top: 0.12in, bottom: 0.12in, left: 0.2in, right: 0.2in),
)
#set text(font: "New Computer Modern", size: 10.5pt, fill: black)
#set par(leading: 0.36em, spacing: 0.4em, justify: false)
#set list(indent: 0.8em, body-indent: 0.3em, spacing: 0.22em)

#let d = json("data.json")
#let has(x) = x != none and x != ""
#let contact = d.contact.filter(x => has(x)).join(" | ")
#let daterange(e) = if has(e.start) and has(e.end) {
  e.start + " -- " + e.end
} else if has(e.end) {
  e.end
} else {
  ""
}
#let section(title) = block(width: 100%, above: 0.9em, below: 0.3em)[
  #text(9.5pt, weight: "bold", upper(title))
  #v(-0.14em)
  #line(length: 100%, stroke: 0.5pt + black)
]

#align(center)[
  #text(15pt, weight: "bold")[#d.name]
  #linebreak()
  #text(9pt)[#contact]
]

#if has(d.summary) [
  #section("Summary")
  #text(size: 9pt)[#d.summary]
]

#if d.experience.len() > 0 [
  #section("Experience")
  #for e in d.experience [
    #v(0.2em)
    #grid(
      columns: (1fr, auto),
      column-gutter: 0.4em,
      [#text(size: 10pt, weight: "bold")[#e.title]#if has(e.company) [ — #e.company]],
      [#align(right)[#text(size: 9pt)[#daterange(e)]]],
    )
    #if has(e.location) [
      #text(style: "italic", size: 9pt)[#e.location]
    ]
    #for b in e.bullets [
      - #text(size: 9pt)[#b]
    ]
    #v(0.1em)
  ]
]

#if d.projects.len() > 0 [
  #section("Projects")
  #for p in d.projects [
    #v(0.2em)
    #text(size: 9.5pt, weight: "bold")[#p.name]#if has(p.description) [#text(size: 9pt, style: "italic")[ | #p.description]]
    #for b in p.bullets [
      - #text(size: 9pt)[#b]
    ]
    #v(0.1em)
  ]
]

#if d.education.len() > 0 [
  #section("Education")
  #for e in d.education [
    #grid(
      columns: (1fr, auto),
      column-gutter: 0.4em,
      [
        #text(size: 10pt, weight: "bold")[#e.institution]
        #if has(e.degree) [#text(size: 9pt)[ — #e.degree]]
        #if has(e.major) [#text(size: 9pt, style: "italic")[#e.major]]
      ],
      [#align(right)[#text(size: 9pt)[#daterange(e)]]],
    )
  ]
]

#if d.certifications.len() > 0 [
  #section("Certifications")
  #for c in d.certifications [
    - #text(size: 9pt)[#c]
  ]
]

#if d.skills.len() > 0 [
  #section("Technical Skills")
  #text(size: 8pt)[#d.skills.join(", ")]
]
