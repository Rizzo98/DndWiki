// /campaigns/[id]/pages/[pageId] — page viewer + DM review/edit/approve/archive,
// visibility, relations and version history.

"use client";

import Link from "next/link";
import { FormEvent, useMemo, useState } from "react";
import { buildLinkIndex, type LinkIndex } from "@/components/linked-text";
import { PageContent } from "@/components/page-content";
import { PageLinkEditor } from "@/components/page-link-editor";
import { RelationsCard } from "@/components/page-relations";
import { Alert, Badge, Button, Card, Field, PageStatusBadge, Select, TextArea, TextInput, VisibilityBadge, fmtDate, fmtPercent } from "@/components/ui";
import { AuthGate, useAuth } from "@/lib/auth";
import { campaignsApi, PAGE_KIND_TITLES, sessionsApi, VISIBILITIES, wikiApi, type Campaign, type PageDetail, type PageSummary, type PageVersion, type Session, type TimelineEvent, type WikiVisibility } from "@/lib/api";
import { errMessage, useAsyncData } from "@/lib/use-async";

/** Static info fields edited through the character GUI form (mirrors
 * wiki-service CharacterAttributes + the page's static info table). */
const CHARACTER_FORM_FIELDS = ["race", "class", "gender", "height", "weight", "age"] as const;

/** Location types selectable in the DM form (mirrors wiki-service
 * LocationType). */
const LOCATION_TYPES = [
  "city", "town", "village", "region", "continent", "world",
  "building", "structure", "dungeon", "wilderness", "other",
] as const;

/** Type-specific attribute groups of the location form, shown/hidden by the
 * selected location_type (mirrors wiki-service applicability + the renderer
 * in components/page-content.tsx). `list` fields are edited one item per
 * line and stored as arrays. */
const LOCATION_TYPE_GROUPS: {
  title: string;
  types: string[];
  fields: { key: string; label: string; list?: boolean }[];
}[] = [
  {
    title: "Settlement",
    types: ["city", "town", "village"],
    fields: [
      { key: "population", label: "Population" },
      { key: "government", label: "Government" },
      { key: "ruler", label: "Ruler" },
      { key: "demographics", label: "Demographics" },
      { key: "economy", label: "Economy" },
      { key: "defenses", label: "Defenses" },
      { key: "religion", label: "Religion" },
      { key: "districts", label: "Districts", list: true },
      { key: "notable_locations", label: "Notable locations", list: true },
    ],
  },
  {
    title: "Region / continent",
    types: ["region", "continent"],
    fields: [
      { key: "capital", label: "Capital" },
      { key: "government", label: "Government" },
      { key: "ruler", label: "Ruler" },
      { key: "terrain", label: "Terrain" },
      { key: "climate", label: "Climate" },
      { key: "notable_locations", label: "Notable locations", list: true },
    ],
  },
  {
    title: "World",
    types: ["world"],
    fields: [
      { key: "terrain", label: "Terrain" },
      { key: "climate", label: "Climate" },
      { key: "planes", label: "Planes" },
      { key: "pantheon", label: "Pantheon" },
      { key: "notable_locations", label: "Notable locations", list: true },
    ],
  },
  {
    title: "Building / structure",
    types: ["building", "structure"],
    fields: [
      { key: "owner", label: "Owner" },
      { key: "purpose", label: "Purpose" },
    ],
  },
  {
    title: "Dungeon",
    types: ["dungeon"],
    fields: [
      { key: "entrance", label: "Entrance" },
      { key: "levels", label: "Levels" },
      { key: "hazards", label: "Hazards" },
    ],
  },
  {
    title: "Wilderness",
    types: ["wilderness"],
    fields: [
      { key: "terrain", label: "Terrain" },
      { key: "climate", label: "Climate" },
      { key: "hazards", label: "Hazards" },
      { key: "flora_fauna", label: "Flora & fauna" },
      { key: "notable_locations", label: "Notable locations", list: true },
    ],
  },
];

/** Enum options for the faction/item/quest GUI forms (mirrors
 * wiki-service app/page_attributes.py). */
const FACTION_TYPES = ["guild", "order", "government", "military", "criminal", "religious", "other"] as const;
const ITEM_TYPES = ["weapon", "armor", "potion", "artifact", "wondrous", "trinket", "other"] as const;
const ITEM_RARITIES = ["common", "uncommon", "rare", "very_rare", "legendary", "artifact"] as const;
const QUEST_STATUSES = ["open", "in_progress", "completed", "failed"] as const;
const EVENT_TYPES = ["battle", "negotiation", "discovery", "quest", "catastrophe", "political", "other"] as const;

function splitLines(value: string): string[] {
  return value.split("\n").map((s) => s.trim()).filter(Boolean);
}

export default function PageDetailPage({ params }: { params: { id: string; pageId: string } }) {
  const { token } = useAuth();
  const { data: campaign } = useAsyncData<Campaign>((t) => campaignsApi.get(t, params.id), [params.id]);
  const { data: page, error, loading, reload } = useAsyncData<PageDetail>((t) => wikiApi.page(t, params.pageId), [params.pageId]);
  const { data: versions, reload: reloadVersions } = useAsyncData<PageVersion[]>((t) => wikiApi.versions(t, params.pageId), [params.pageId]);
  // Sessions of the campaign, so session references render by NAME not id.
  const { data: sessions } = useAsyncData<Session[]>((t) => sessionsApi.list(t, params.id), [params.id]);
  // Timeline entries of the campaign: event pages mirror their in-world date
  // on the linked entry and show its approval state.
  const { data: timelineEvents } = useAsyncData<TimelineEvent[]>((t) => wikiApi.timeline(t, params.id), [params.id]);
  const sessionNames = useMemo(() => {
    const names: Record<string, string> = {};
    for (const s of sessions ?? []) {
      names[s.id] = s.title?.trim() || (s.session_no ? `Session ${s.session_no}` : s.id);
    }
    return names;
  }, [sessions]);

  // Campaign page index: entity names in the page text link to their pages.
  const { data: pages } = useAsyncData<PageSummary[]>((t) => wikiApi.pages(t, params.id, { limit: 500 }), [params.id]);
  const linkIndex: LinkIndex | null = useMemo(() => buildLinkIndex(pages ?? []), [pages]);

  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  // character portrait upload (DM only)
  const [imageBusy, setImageBusy] = useState(false);

  // edit page
  const [editing, setEditing] = useState(false);
  const [editForm, setEditForm] = useState({
    title: "",
    slug: "",
    // character GUI fields
    characterType: "npc" as "npc" | "player",
    race: "",
    class: "",
    gender: "",
    height: "",
    weight: "",
    age: "",
    physicalLook: "",
    personality: "",
    facts: "",
    aliases: "",
    changeNote: "",
    // location GUI fields (type-specific groups)
    locationType: "city" as string,
    region: "",
    latitude: "",
    longitude: "",
    founded: "",
    overview: "",
    history: "",
    population: "",
    government: "",
    ruler: "",
    demographics: "",
    economy: "",
    defenses: "",
    religion: "",
    districts: "",
    notable_locations: "",
    capital: "",
    terrain: "",
    climate: "",
    planes: "",
    pantheon: "",
    owner: "",
    purpose: "",
    entrance: "",
    levels: "",
    hazards: "",
    flora_fauna: "",
    // faction / item / quest GUI fields
    simpleType: "",
    rarity: "",
    leader: "",
    headquarters: "",
    giver: "",
    reward: "",
    // event GUI fields
    eventType: "other",
    inWorldDate: "",
    participants: "",
    // raw JSON fallback for unknown kinds
    contentJson: "",
  });

  if (loading) return <p className="text-sm text-slate-400">Loading page…</p>;
  if (error || !page) return <Alert tone="error">{error ?? "Page not found"}</Alert>;

  const isDm = campaign?.my_role === "dm";
  const isCharacter = page.kind === "character";
  const isLocation = page.kind === "location";
  const isSimpleKind = page.kind === "faction" || page.kind === "item" || page.kind === "quest";
  const isEvent = page.kind === "event";

  async function run(fn: () => Promise<unknown>, successMsg: string) {
    if (!token) return;
    setBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(successMsg);
      reload();
      reloadVersions();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function uploadImage(file: File) {
    if (!token || !page) return;
    setImageBusy(true);
    setFormError(null);
    setNotice(null);
    try {
      await wikiApi.uploadImage(token, page.id, file);
      setNotice("Character portrait updated.");
      reload();
    } catch (err) {
      setFormError(errMessage(err));
    } finally {
      setImageBusy(false);
    }
  }

  function startEdit() {
    if (!page) return;
    const attrs = (page.content_json?.attributes ?? {}) as Record<string, unknown>;
    const str = (v: unknown) => (typeof v === "string" ? v : "");
    const strings = (value: unknown) =>
      Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
    const num = (v: unknown) => (typeof v === "number" ? String(v) : "");
    setEditForm({
      title: page.title,
      slug: page.slug,
      characterType: attrs.character_type === "player" ? "player" : "npc",
      race: str(attrs.race),
      class: str(attrs.class),
      gender: str(attrs.gender),
      height: str(attrs.height),
      weight: str(attrs.weight),
      age: str(attrs.age),
      physicalLook: str(page.content_json?.physical_look),
      personality: str(page.content_json?.personality),
      facts: strings(page.content_json?.facts).join("\n"),
      aliases: strings(page.content_json?.aliases).join("\n"),
      changeNote: "",
      // location GUI fields
      locationType: typeof attrs.location_type === "string" ? attrs.location_type : "city",
      region: str(attrs.region),
      latitude: num(attrs.latitude),
      longitude: num(attrs.longitude),
      founded: str(attrs.founded),
      overview: str(page.content_json?.summary),
      history: str(page.content_json?.history),
      population: str(attrs.population),
      government: str(attrs.government),
      ruler: str(attrs.ruler),
      demographics: str(attrs.demographics),
      economy: str(attrs.economy),
      defenses: str(attrs.defenses),
      religion: str(attrs.religion),
      districts: strings(attrs.districts).join("\n"),
      notable_locations: strings(attrs.notable_locations).join("\n"),
      capital: str(attrs.capital),
      terrain: str(attrs.terrain),
      climate: str(attrs.climate),
      planes: str(attrs.planes),
      pantheon: str(attrs.pantheon),
      owner: str(attrs.owner),
      purpose: str(attrs.purpose),
      entrance: str(attrs.entrance),
      levels: str(attrs.levels),
      hazards: str(attrs.hazards),
      flora_fauna: str(attrs.flora_fauna),
      // faction / item / quest
      simpleType: str(attrs.faction_type) || str(attrs.item_type) || str(attrs.quest_status),
      rarity: str(attrs.rarity),
      leader: str(attrs.leader),
      headquarters: str(attrs.headquarters),
      giver: str(attrs.giver),
      reward: str(attrs.reward),
      // event
      eventType: str(attrs.event_type) || "other",
      inWorldDate: str(attrs.in_world_date),
      participants: strings(attrs.participants).join("\n"),
      contentJson: JSON.stringify(page.content_json ?? {}, null, 2),
    });
    setEditing(true);
  }

  function toggleEdit() {
    if (editing) {
      setEditing(false);
      return;
    }
    startEdit();
  }

  /** Rebuild a character's content_json from the GUI form, preserving the
   * keys the form does not manage (language, session_references, image_uri,
   * unknown extras). */
  function buildCharacterContent(
    form: typeof editForm,
    existing: Record<string, unknown>,
  ): Record<string, unknown> {
    const next: Record<string, unknown> = { ...existing };
    delete next.summary;
    delete next.physical_look;
    delete next.personality;
    delete next.facts;
    delete next.aliases;
    delete next.attributes;
    if (form.physicalLook.trim()) next.physical_look = form.physicalLook.trim();
    if (form.personality.trim()) next.personality = form.personality.trim();
    const facts = splitLines(form.facts);
    if (facts.length) next.facts = facts;
    const aliases = splitLines(form.aliases);
    if (aliases.length) next.aliases = aliases;
    const attributes: Record<string, unknown> = {};
    if (form.characterType) attributes.character_type = form.characterType;
    for (const key of CHARACTER_FORM_FIELDS) {
      const value = form[key].trim();
      if (value) attributes[key] = value;
    }
    if (Object.keys(attributes).length) next.attributes = attributes;
    return next;
  }

  /** Rebuild a location's content_json from the GUI form, preserving keys
   * the form does not manage (language, session_references, image_uri,
   * unknown extras). Only fields valid for the chosen location_type are
   * emitted — the wiki-service schema rejects the rest. */
  function buildLocationContent(
    form: typeof editForm,
    existing: Record<string, unknown>,
  ): Record<string, unknown> {
    const next: Record<string, unknown> = { ...existing };
    delete next.summary;
    delete next.history;
    delete next.facts;
    delete next.aliases;
    delete next.attributes;
    if (form.overview.trim()) next.summary = form.overview.trim();
    if (form.history.trim()) next.history = form.history.trim();
    const facts = splitLines(form.facts);
    if (facts.length) next.facts = facts;
    const aliases = splitLines(form.aliases);
    if (aliases.length) next.aliases = aliases;

    const attributes: Record<string, unknown> = { location_type: form.locationType };
    // Defensive: only touch string fields — a form/group key mismatch must
    // never crash the save (it would surface as an attribute omission).
    const setIf = (key: string, value: unknown) => {
      if (typeof value === "string" && value.trim()) attributes[key] = value.trim();
    };
    const setListIf = (key: string, value: unknown) => {
      if (typeof value !== "string") return;
      const items = splitLines(value);
      if (items.length) attributes[key] = items;
    };
    // common fields
    setIf("region", form.region);
    const latitude = Number(form.latitude);
    if (form.latitude.trim() !== "" && Number.isFinite(latitude)) attributes.latitude = latitude;
    const longitude = Number(form.longitude);
    if (form.longitude.trim() !== "" && Number.isFinite(longitude)) attributes.longitude = longitude;
    setIf("founded", form.founded);
    // type-specific groups (only the ones valid for this type)
    for (const group of LOCATION_TYPE_GROUPS) {
      if (!group.types.includes(form.locationType)) continue;
      for (const field of group.fields) {
        if (field.list) setListIf(field.key, form[field.key as keyof typeof form] as string);
        else setIf(field.key, form[field.key as keyof typeof form] as string);
      }
    }
    if (Object.keys(attributes).length) next.attributes = attributes;
    return next;
  }

  /** Rebuild a faction/item/quest content_json from the GUI form, preserving
   * keys the form does not manage (language, session_references, image_uri,
   * unknown extras). */
  function buildSimpleContent(
    kind: string,
    form: typeof editForm,
    existing: Record<string, unknown>,
  ): Record<string, unknown> {
    const next: Record<string, unknown> = { ...existing };
    delete next.summary;
    delete next.facts;
    delete next.aliases;
    delete next.attributes;
    if (form.overview.trim()) next.summary = form.overview.trim();
    const facts = splitLines(form.facts);
    if (facts.length) next.facts = facts;
    const aliases = splitLines(form.aliases);
    if (aliases.length) next.aliases = aliases;

    const attributes: Record<string, unknown> = {};
    if (kind === "faction") {
      if (form.simpleType) attributes.faction_type = form.simpleType;
      if (form.leader.trim()) attributes.leader = form.leader.trim();
      if (form.headquarters.trim()) attributes.headquarters = form.headquarters.trim();
    } else if (kind === "item") {
      if (form.simpleType) attributes.item_type = form.simpleType;
      if (form.rarity) attributes.rarity = form.rarity;
      if (form.owner.trim()) attributes.owner = form.owner.trim();
    } else if (kind === "quest") {
      if (form.simpleType) attributes.quest_status = form.simpleType;
      if (form.giver.trim()) attributes.giver = form.giver.trim();
      if (form.reward.trim()) attributes.reward = form.reward.trim();
    }
    if (Object.keys(attributes).length) next.attributes = attributes;
    return next;
  }

  /** Rebuild an event page's content_json from the GUI form, preserving keys
   * the form does not manage (language, session_references, image_uri, ...). */
  function buildEventContent(
    form: typeof editForm,
    existing: Record<string, unknown>,
  ): Record<string, unknown> {
    const next: Record<string, unknown> = { ...existing };
    delete next.summary;
    delete next.facts;
    delete next.aliases;
    delete next.attributes;
    if (form.overview.trim()) next.summary = form.overview.trim();
    const facts = splitLines(form.facts);
    if (facts.length) next.facts = facts;
    const aliases = splitLines(form.aliases);
    if (aliases.length) next.aliases = aliases;

    const attributes: Record<string, unknown> = {
      event_type: form.eventType || "other",
    };
    if (form.inWorldDate.trim()) attributes.in_world_date = form.inWorldDate.trim();
    const participants = splitLines(form.participants);
    if (participants.length) attributes.participants = participants;
    next.attributes = attributes;
    return next;
  }

  async function saveEdit(e: FormEvent) {
    e.preventDefault();
    if (!token || !page) return;
    let contentJson: Record<string, unknown> | undefined;
    if (isCharacter) {
      contentJson = buildCharacterContent(editForm, page.content_json ?? {});
    } else if (isLocation) {
      contentJson = buildLocationContent(editForm, page.content_json ?? {});
    } else if (isSimpleKind) {
      contentJson = buildSimpleContent(page.kind, editForm, page.content_json ?? {});
    } else if (isEvent) {
      contentJson = buildEventContent(editForm, page.content_json ?? {});
    } else if (editForm.contentJson.trim()) {
      try {
        contentJson = JSON.parse(editForm.contentJson);
      } catch {
        setFormError("content_json must be valid JSON.");
        return;
      }
    }
    const newDate = editForm.inWorldDate.trim() || null;
    await run(
      () =>
        wikiApi.updatePage(token, page.id, {
          title: editForm.title.trim() || undefined,
          slug: editForm.slug.trim() || undefined,
          content_json: contentJson,
          change_note: editForm.changeNote.trim() || undefined,
        }),
      "Page updated (new version snapshot created).",
    );
    // Event pages mirror their in-world date on the timeline entry: keep the
    // two in sync when a linked entry exists (DM only — players cannot edit).
    if (isEvent && isDm && token) {
      const linked = timelineEvents?.find((te) => te.page_id === page.id);
      if (linked && (linked.in_world_date ?? null) !== newDate) {
        try {
          await wikiApi.updateTimelineEvent(token, linked.id, { in_world_date: newDate });
        } catch (err) {
          setNotice("Page saved, but the timeline date could not be synced: " + errMessage(err));
        }
      }
    }
    setEditing(false);
  }

  function setChar<K extends keyof typeof editForm>(key: K, value: (typeof editForm)[K]) {
    setEditForm((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <AuthGate>
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href={`/campaigns/${params.id}`} className="text-xs text-slate-500 hover:text-slate-300">← back to campaign</Link>
          <h1 className="mt-1 text-3xl font-bold">{page.title}</h1>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
            <Badge tone="slate">{PAGE_KIND_TITLES[page.kind] ?? page.kind}</Badge>
            <PageStatusBadge status={page.status} />
            <VisibilityBadge visibility={page.visibility} />
            <span className="text-slate-500">slug: {page.slug}</span>
            <span className="text-slate-500">updated {fmtDate(page.updated_at)}</span>
            {page.confidence !== null && page.confidence !== undefined ? (
              <span className="text-slate-500">confidence {fmtPercent(page.confidence)}</span>
            ) : null}
          </div>
        </div>
      </div>

      {/* DM actions as a compact top menu */}
      {isDm ? (
        <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
          <span className="text-xs font-bold uppercase tracking-wide text-slate-500">DM actions</span>
          <Button variant="secondary" onClick={toggleEdit}>
            {editing ? "Cancel edit" : "Edit"}
          </Button>
          {page.status !== "published" ? (
            <Button onClick={() => run(() => wikiApi.approve(token!, page.id), "Page approved and published.")} disabled={busy}>
              Approve & publish
            </Button>
          ) : null}
          {page.status !== "archived" ? (
            <Button variant="danger" onClick={() => run(() => wikiApi.archive(token!, page.id), "Page archived.")} disabled={busy}>
              Archive
            </Button>
          ) : null}
          <label className="ml-auto flex items-center gap-2 text-xs text-slate-400">
            Visibility
            <Select
              className="w-40"
              value={page.visibility}
              onChange={(e) => run(() => wikiApi.visibility(token!, page.id, e.target.value as WikiVisibility), "Visibility updated.")}
              disabled={busy}
            >
              {VISIBILITIES.map((v) => (
                <option key={v} value={v}>{v}</option>
              ))}
            </Select>
          </label>
        </Card>
      ) : null}

      {notice ? <Alert tone="success">{notice}</Alert> : null}
      {formError ? <Alert tone="error">{formError}</Alert> : null}

      {isEvent ? (() => {
        const linked = timelineEvents?.find((te) => te.page_id === page.id);
        return (
          <Card className="flex flex-wrap items-center gap-x-4 gap-y-2 py-3">
            <span className="text-xs font-bold uppercase tracking-wide text-slate-500">Timeline</span>
            {linked ? (
              <>
                {linked.in_world_date ? (
                  <span className="text-sm text-slate-200">{linked.in_world_date}</span>
                ) : (
                  <span className="text-sm text-slate-500">No in-world date set</span>
                )}
                <Badge tone={linked.approved ? "green" : "amber"}>
                  {linked.approved ? "approved" : "pending DM approval"}
                </Badge>
                <Link
                  href={"/campaigns/" + params.id + "?tab=timeline"}
                  className="text-xs text-slate-500 hover:text-slate-300"
                >
                  ← back to timeline
                </Link>
              </>
            ) : (
              <span className="text-sm text-slate-500">Not linked to the campaign timeline yet.</span>
            )}
          </Card>
        );
      })() : null}

      {!editing ? (
        <Card>
          <h2 className="mb-4 text-sm font-semibold uppercase tracking-wide text-slate-500">Content</h2>
          <PageContent
            content={page.content_json ?? {}}
            campaignId={params.id}
            kind={page.kind}
            sessionNames={sessionNames}
            imageUrl={page.image_url}
            onUploadImage={isDm ? uploadImage : undefined}
            uploadingImage={imageBusy}
            linkIndex={linkIndex}
            currentPageId={page.id}
          />
        </Card>
      ) : (
        <Card>
          <h2 className="mb-4 text-lg font-semibold">
            {isCharacter
              ? "Edit character"
              : isLocation
                ? "Edit location"
                : isSimpleKind
                  ? "Edit " + ((PAGE_KIND_TITLES as Record<string, string>)[page.kind] ?? "page")
                  : isEvent
                    ? "Edit event"
                    : "Edit page"}
          </h2>
          <form onSubmit={saveEdit} className="space-y-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Title">
                <TextInput required value={editForm.title} onChange={(e) => setChar("title", e.target.value)} />
              </Field>
              <Field label="Slug">
                <TextInput value={editForm.slug} onChange={(e) => setChar("slug", e.target.value)} />
              </Field>
            </div>

            {isCharacter ? (
              <>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field label="Character type">
                    <Select
                      value={editForm.characterType}
                      onChange={(e) => setChar("characterType", e.target.value as "npc" | "player")}
                    >
                      <option value="npc">NPC</option>
                      <option value="player">Player character</option>
                    </Select>
                  </Field>
                  <Field label="Race">
                    <TextInput value={editForm.race} onChange={(e) => setChar("race", e.target.value)} />
                  </Field>
                  <Field label="Class">
                    <TextInput value={editForm.class} onChange={(e) => setChar("class", e.target.value)} />
                  </Field>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field label="Gender">
                    <TextInput value={editForm.gender} onChange={(e) => setChar("gender", e.target.value)} />
                  </Field>
                  <Field label="Height">
                    <TextInput value={editForm.height} onChange={(e) => setChar("height", e.target.value)} placeholder="1.78 m" />
                  </Field>
                  <Field label="Weight">
                    <TextInput value={editForm.weight} onChange={(e) => setChar("weight", e.target.value)} placeholder="82 kg" />
                  </Field>
                </div>
                <Field label="Age">
                  <TextInput value={editForm.age} onChange={(e) => setChar("age", e.target.value)} />
                </Field>
                <Field label="Physical look" hint="Physical description of the character">
                  <PageLinkEditor rows={4} value={editForm.physicalLook} onChange={(v) => setChar("physicalLook", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Personality">
                  <PageLinkEditor rows={4} value={editForm.personality} onChange={(v) => setChar("personality", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Facts" hint="One fact per line — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.facts} onChange={(v) => setChar("facts", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Aliases" hint="One alias per line">
                  <TextArea rows={2} value={editForm.aliases} onChange={(e) => setChar("aliases", e.target.value)} />
                </Field>
              </>
            ) : isLocation ? (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Location type">
                    <Select
                      value={editForm.locationType}
                      onChange={(e) => setChar("locationType", e.target.value)}
                    >
                      {LOCATION_TYPES.map((t) => (
                        <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Founded">
                    <TextInput value={editForm.founded} onChange={(e) => setChar("founded", e.target.value)} placeholder="1290 DR" />
                  </Field>
                </div>
                <div className="grid gap-3 sm:grid-cols-3">
                  <Field label="Region">
                    <TextInput value={editForm.region} onChange={(e) => setChar("region", e.target.value)} placeholder="Containing region" />
                  </Field>
                  <Field label="Latitude">
                    <TextInput value={editForm.latitude} onChange={(e) => setChar("latitude", e.target.value)} placeholder="40.5" />
                  </Field>
                  <Field label="Longitude">
                    <TextInput value={editForm.longitude} onChange={(e) => setChar("longitude", e.target.value)} placeholder="18.2" />
                  </Field>
                </div>
                <Field label="Overview" hint="Short narrative description — type # to link a page">
                  <PageLinkEditor rows={3} value={editForm.overview} onChange={(v) => setChar("overview", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="History" hint="Founding, wars, famous events — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.history} onChange={(v) => setChar("history", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Facts" hint="One fact per line — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.facts} onChange={(v) => setChar("facts", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Aliases" hint="One alias per line">
                  <TextArea rows={2} value={editForm.aliases} onChange={(e) => setChar("aliases", e.target.value)} />
                </Field>
                {LOCATION_TYPE_GROUPS.filter((g) => g.types.includes(editForm.locationType)).map((group) => (
                  <div key={group.title} className="space-y-3 rounded-lg border border-slate-800 bg-slate-950/30 p-3">
                    <h4 className="text-xs font-bold uppercase tracking-wide text-slate-500">{group.title}</h4>
                    <div className="grid gap-3 sm:grid-cols-2">
                      {group.fields.map((field) =>
                        field.list ? (
                          <Field key={field.key} label={field.label} hint="One per line — type # to link a page">
                            <PageLinkEditor rows={2} value={editForm[field.key as keyof typeof editForm] as string} onChange={(v) => setChar(field.key as keyof typeof editForm, v)} pages={pages ?? []} excludePageId={page.id} />
                          </Field>
                        ) : (
                          <Field key={field.key} label={field.label}>
                            <TextInput value={editForm[field.key as keyof typeof editForm] as string} onChange={(e) => setChar(field.key as keyof typeof editForm, e.target.value)} />
                          </Field>
                        ),
                      )}
                    </div>
                  </div>
                ))}
              </>
            ) : isSimpleKind ? (
              <>
                <Field label="Overview" hint="Short narrative description — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.overview} onChange={(v) => setChar("overview", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Facts" hint="One fact per line — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.facts} onChange={(v) => setChar("facts", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Aliases" hint="One alias per line">
                  <TextArea rows={2} value={editForm.aliases} onChange={(e) => setChar("aliases", e.target.value)} />
                </Field>

                {page.kind === "faction" ? (
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label="Faction type">
                      <Select value={editForm.simpleType} onChange={(e) => setChar("simpleType", e.target.value)}>
                        {FACTION_TYPES.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </Select>
                    </Field>
                    <Field label="Leader">
                      <TextInput value={editForm.leader} onChange={(e) => setChar("leader", e.target.value)} placeholder="Who leads it" />
                    </Field>
                    <Field label="Headquarters">
                      <TextInput value={editForm.headquarters} onChange={(e) => setChar("headquarters", e.target.value)} placeholder="Where it operates from" />
                    </Field>
                  </div>
                ) : page.kind === "item" ? (
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label="Item type">
                      <Select value={editForm.simpleType} onChange={(e) => setChar("simpleType", e.target.value)}>
                        {ITEM_TYPES.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </Select>
                    </Field>
                    <Field label="Rarity">
                      <Select value={editForm.rarity} onChange={(e) => setChar("rarity", e.target.value)}>
                        {ITEM_RARITIES.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </Select>
                    </Field>
                    <Field label="Owner">
                      <TextInput value={editForm.owner} onChange={(e) => setChar("owner", e.target.value)} placeholder="Who holds it" />
                    </Field>
                  </div>
                ) : (
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label="Quest status">
                      <Select value={editForm.simpleType} onChange={(e) => setChar("simpleType", e.target.value)}>
                        {QUEST_STATUSES.map((t) => (
                          <option key={t} value={t}>{t}</option>
                        ))}
                      </Select>
                    </Field>
                    <Field label="Given by">
                      <TextInput value={editForm.giver} onChange={(e) => setChar("giver", e.target.value)} placeholder="Who assigned it" />
                    </Field>
                    <Field label="Reward">
                      <TextInput value={editForm.reward} onChange={(e) => setChar("reward", e.target.value)} placeholder="What it pays" />
                    </Field>
                  </div>
                )}
              </>
            ) : isEvent ? (
              <>
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label="Event type">
                    <Select value={editForm.eventType} onChange={(e) => setChar("eventType", e.target.value)}>
                      {EVENT_TYPES.map((t) => (
                        <option key={t} value={t}>{t.charAt(0).toUpperCase() + t.slice(1)}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="In-world date" hint="Mirrored on the campaign timeline">
                    <TextInput value={editForm.inWorldDate} onChange={(e) => setChar("inWorldDate", e.target.value)} placeholder="17 Ches 1492 DR" />
                  </Field>
                </div>
                <Field label="Overview" hint="What happened — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.overview} onChange={(v) => setChar("overview", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Participants" hint="One per line — type # to link a page">
                  <PageLinkEditor rows={3} value={editForm.participants} onChange={(v) => setChar("participants", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Facts" hint="One fact per line — type # to link a page">
                  <PageLinkEditor rows={4} value={editForm.facts} onChange={(v) => setChar("facts", v)} pages={pages ?? []} excludePageId={page.id} />
                </Field>
                <Field label="Aliases" hint="One alias per line">
                  <TextArea rows={2} value={editForm.aliases} onChange={(e) => setChar("aliases", e.target.value)} />
                </Field>
              </>
            ) : (
              <Field label="content_json" hint="Edits create an immutable version snapshot">
                <TextArea rows={10} className="font-mono text-xs" value={editForm.contentJson} onChange={(e) => setChar("contentJson", e.target.value)} />
              </Field>
            )}

            <Field label="Change note">
              <TextInput value={editForm.changeNote} onChange={(e) => setChar("changeNote", e.target.value)} placeholder="Why this change?" />
            </Field>
            <div className="flex gap-2">
              <Button type="submit" disabled={busy}>Save version</Button>
              <Button variant="ghost" onClick={() => setEditing(false)}>Cancel</Button>
            </div>
          </form>
        </Card>
      )}

      <RelationsCard campaignId={params.id} pageId={page.id} token={token} isDm={isDm} />

      <Card>
        <h2 className="mb-4 text-lg font-semibold">Version history</h2>
        {versions && versions.length === 0 ? (
          <p className="text-sm text-slate-500">No versions yet.</p>
        ) : (
          <div className="divide-y divide-slate-800">
            {versions?.map((v) => {
              // The portrait preview is only valid when the snapshot's image is
              // still the current one (no presigned URL exists for old images).
              const sameImage = v.content_json?.image_uri === page.content_json?.image_uri;
              return (
                <div key={v.id} className="py-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-sm font-semibold text-slate-200">v{v.version_no}</span>
                    <span className="text-xs text-slate-500">{fmtDate(v.created_at)}</span>
                  </div>
                  {v.change_note ? <p className="mt-1 text-sm text-slate-400">{v.change_note}</p> : null}
                  <details className="mt-2">
                    <summary className="cursor-pointer text-xs text-slate-500 hover:text-slate-300">preview content</summary>
                    <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/40 p-4">
                      <PageContent
                        content={v.content_json}
                        campaignId={params.id}
                        kind={page.kind}
                        sessionNames={sessionNames}
                        imageUrl={sameImage ? page.image_url : null}
                        hidePortrait={!sameImage}
                        linkIndex={linkIndex}
                        currentPageId={page.id}
                      />
                    </div>
                  </details>
                </div>
              );
            })}
          </div>
        )}
      </Card>
    </div>
    </AuthGate>
  );
}