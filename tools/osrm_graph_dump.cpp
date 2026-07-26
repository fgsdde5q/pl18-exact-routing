#include "extractor/edge_based_graph_factory.hpp"
#include "extractor/files.hpp"
#include "extractor/node_data_container.hpp"
#include "extractor/packed_osm_ids.hpp"
#include "extractor/segment_data_container.hpp"
#include "util/coordinate.hpp"
#include "util/coordinate_calculation.hpp"
#include "util/typedefs.hpp"

#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{
using namespace osrm;

std::filesystem::path with_suffix(const std::filesystem::path &base, const std::string &suffix)
{
    return std::filesystem::path(base.string() + suffix);
}

template <typename Alias> auto value(const Alias &input)
{
    return util::from_alias<typename Alias::value_type>(input);
}

struct DirectedGeometry
{
    OSMNodeID first_from;
    OSMNodeID first_to;
    OSMNodeID last_from;
    OSMNodeID last_to;
};
} // namespace

int main(int argc, char *argv[])
{
    using namespace osrm;
    using namespace osrm::extractor;

    if (argc != 3)
    {
        std::cerr << "usage: osrm-graph-dump BASE.osrm OUTPUT_DIRECTORY\n";
        return 2;
    }

    const std::filesystem::path base = argv[1];
    const std::filesystem::path output = argv[2];
    std::filesystem::create_directories(output);

    std::vector<util::Coordinate> coordinates;
    PackedOSMIDs osm_node_ids;
    files::readNodes(with_suffix(base, ".nbg_nodes"), coordinates, osm_node_ids);

    SegmentDataContainer segment_data;
    files::readSegmentData(with_suffix(base, ".geometry"), segment_data);

    EdgeBasedNodeDataContainer node_data;
    files::readNodeData(with_suffix(base, ".ebg_nodes"), node_data);

    EdgeID number_of_edge_based_nodes = 0;
    std::vector<EdgeBasedEdge> edge_based_edges;
    std::uint32_t connectivity_checksum = 0;
    files::readEdgeBasedGraph(with_suffix(base, ".ebg"),
                              number_of_edge_based_nodes,
                              edge_based_edges,
                              connectivity_checksum);

    std::vector<TurnPenalty> turn_durations;
    files::readTurnDurationPenalty(with_suffix(base, ".turn_duration_penalties"), turn_durations);
    std::vector<lookup::TurnIndexBlock> turn_indexes;
    files::readTurnPenaltiesIndex(with_suffix(base, ".turn_penalties_index"), turn_indexes);

    if (node_data.NumberOfNodes() != number_of_edge_based_nodes)
        throw std::runtime_error("edge-based node count mismatch");
    if (turn_durations.size() != edge_based_edges.size() ||
        turn_indexes.size() != edge_based_edges.size())
        throw std::runtime_error("turn metadata count mismatch");

    std::ofstream segments(output / "osrm-directed-segments.tsv");
    std::ofstream node_endpoints(output / "osrm-edge-node-endpoints.tsv");
    segments << "edge_based_node_id\tgeometry_segment_ordinal\tfrom_node_id\tto_node_id"
                "\tfrom_longitude\tfrom_latitude\tto_longitude\tto_latitude"
                "\tlength_m\tduration_ds\n";
    node_endpoints << "edge_based_node_id\tfirst_from_node_id\tfirst_to_node_id"
                      "\tlast_from_node_id\tlast_to_node_id\n";
    segments << std::fixed;

    std::vector<DirectedGeometry> directed_geometry(number_of_edge_based_nodes);
    for (NodeID edge_based_node_id = 0; edge_based_node_id < number_of_edge_based_nodes;
         ++edge_based_node_id)
    {
        const auto geometry_id = node_data.GetGeometryID(edge_based_node_id);
        const auto write_geometry = [&](const auto &geometry, const auto &durations)
        {
            if (geometry.size() < 2 || durations.size() + 1 != geometry.size())
                throw std::runtime_error("invalid directed geometry");

            auto duration = durations.begin();
            for (std::size_t ordinal = 0; ordinal + 1 < geometry.size(); ++ordinal, ++duration)
            {
                const auto from_internal = geometry[ordinal];
                const auto to_internal = geometry[ordinal + 1];
                const auto from_osm = osm_node_ids[from_internal];
                const auto to_osm = osm_node_ids[to_internal];
                const auto length = util::coordinate_calculation::greatCircleDistance(
                    coordinates[from_internal], coordinates[to_internal]);
                segments << edge_based_node_id << '\t' << ordinal << '\t' << value(from_osm)
                         << '\t' << value(to_osm) << '\t' << std::setprecision(6)
                         << static_cast<double>(util::toFloating(coordinates[from_internal].lon))
                         << '\t'
                         << static_cast<double>(util::toFloating(coordinates[from_internal].lat))
                         << '\t'
                         << static_cast<double>(util::toFloating(coordinates[to_internal].lon))
                         << '\t'
                         << static_cast<double>(util::toFloating(coordinates[to_internal].lat))
                         << '\t' << std::setprecision(3) << length << '\t' << value(*duration)
                         << '\n';
            }

            directed_geometry[edge_based_node_id] = {
                osm_node_ids[geometry.front()],
                osm_node_ids[geometry[1]],
                osm_node_ids[geometry[geometry.size() - 2]],
                osm_node_ids[geometry.back()],
            };
        };
        if (geometry_id.forward)
            write_geometry(segment_data.GetForwardGeometry(geometry_id.id),
                           segment_data.GetForwardDurations(geometry_id.id));
        else
            write_geometry(segment_data.GetReverseGeometry(geometry_id.id),
                           segment_data.GetReverseDurations(geometry_id.id));
        const auto &record = directed_geometry[edge_based_node_id];
        node_endpoints << edge_based_node_id << '\t' << value(record.first_from) << '\t'
                       << value(record.first_to) << '\t' << value(record.last_from) << '\t'
                       << value(record.last_to) << '\n';
    }

    std::ofstream turns(output / "osrm-turn-states.tsv");
    turns << "incoming_edge_based_node_id\toutgoing_edge_based_node_id\tfrom_node_id"
             "\tvia_node_id\tto_node_id\tturn_duration_ds\trestriction_status\n";
    std::uint64_t directed_turn_count = 0;
    for (std::size_t index = 0; index < edge_based_edges.size(); ++index)
    {
        const auto &edge = edge_based_edges[index];
        const auto &turn = turn_indexes[edge.data.turn_id];
        const auto duration = value(turn_durations[edge.data.turn_id]);
        if (edge.data.forward)
        {
            turns << edge.source << '\t' << edge.target << '\t'
                  << value(osm_node_ids[turn.from_id]) << '\t'
                  << value(osm_node_ids[turn.via_id]) << '\t'
                  << value(osm_node_ids[turn.to_id]) << '\t' << duration << "\tallowed\n";
            ++directed_turn_count;
        }
        if (edge.data.backward)
        {
            turns << edge.target << '\t' << edge.source << '\t'
                  << value(osm_node_ids[turn.to_id]) << '\t'
                  << value(osm_node_ids[turn.via_id]) << '\t'
                  << value(osm_node_ids[turn.from_id]) << '\t' << duration << "\tallowed\n";
            ++directed_turn_count;
        }
    }

    std::ofstream summary(output / "osrm-export-summary.txt");
    summary << "node_based_nodes=" << coordinates.size() << '\n'
            << "edge_based_nodes=" << number_of_edge_based_nodes << '\n'
            << "stored_edge_based_edges=" << edge_based_edges.size() << '\n'
            << "directed_turn_states=" << directed_turn_count << '\n'
            << "connectivity_checksum=" << connectivity_checksum << '\n';

    return 0;
}
