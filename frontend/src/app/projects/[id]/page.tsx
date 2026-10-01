"use client";
import { useParams } from "next/navigation";
import { ResourceList } from "@/components/resource-list";

export default function ProjectPage() {
  const { id } = useParams<{ id: string }>();
  return <main className="mx-auto max-w-5xl px-6 py-12"><ResourceList projectId={id} /></main>;
}