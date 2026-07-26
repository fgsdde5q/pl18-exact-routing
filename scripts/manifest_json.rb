#!/usr/bin/env ruby

require "json"
require "yaml"

abort "usage: #{$PROGRAM_NAME} MANIFEST" unless ARGV.length == 1

manifest = YAML.safe_load(
  File.read(ARGV.fetch(0), encoding: "UTF-8"),
  permitted_classes: [],
  aliases: false
)

puts JSON.generate(manifest)
