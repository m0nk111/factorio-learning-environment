FLE = FLE or {}
FLE.utils = FLE.utils or {}
FLE.actions = FLE.actions or {}
-- Factorio 2.1 removed several inventory defines (assembling_machine_*,
-- furnace_*, reactor_*, lab_source, mining_drill_input, storage_tank, ...). Build
-- the list densely: a sparse literal would make ipairs stop at the first nil and
-- silently skip every later inventory.
local function inventory_list()
    local d = defines.inventory
    local list = {}
    local function add(id) if id then list[#list + 1] = id end end
    add(d.chest)
    add(d.crafter_input)
    add(d.crafter_output)
    add(d.crafter_modules)
    add(d.fuel)
    add(d.burnt_result)
    add(d.lab_input)
    add(d.lab_modules)
    add(d.item_main)
    add(d.robot_cargo)
    add(d.robot_repair)
    add(d.car_trunk)
    add(d.car_ammo)
    add(d.roboport_material)
    add(d.roboport_robot)
    add(d.turret_ammo)
    add(d.cargo_wagon)
    add(d.rocket_silo_rocket)
    return list
end

-- Helper function to check all possible inventories of an entity
local function get_entity_item_count(entity, item_name)
        local inventory_types = inventory_list()

    local total_count = 0
    for _, inv_type in ipairs(inventory_types) do
        local inventory = entity.get_inventory(inv_type)
        if inventory then
            total_count = total_count + entity.get_item_count(item_name)
        end
    end
    return total_count
end

-- Helper function to remove items from any valid inventory
local function remove_items_from_entity(entity, stack)
        local inventory_types = inventory_list()

    local items_remaining = stack.count
    local total_removed = 0

    for _, inv_type in ipairs(inventory_types) do
        if items_remaining <= 0 then
            break
        end

        local inventory = entity.get_inventory(inv_type)
        if inventory then
            local current_stack = {name = stack.name, count = items_remaining}
            local removed = entity.remove_item(current_stack)
            total_removed = total_removed + removed
            items_remaining = items_remaining - removed
        end
    end

    return total_removed
end

FLE.actions.extract_item = function(player_index, extract_item, count, x, y, source_name)
    -- Ensure we have a valid character, recreating if necessary
    local player = FLE.utils.ensure_valid_character(player_index)
    local position = {x=x, y=y}
    local surface = player.surface

    -- First validate the request
    if count <= 0 then
        error("\"Invalid count: must be greater than 0\"")
    end

    -- Find all entities in range
    local search_radius = 10
    local area = {{position.x - search_radius, position.y - search_radius},
                  {position.x + search_radius, position.y + search_radius}}
    local buildings = nil

    if source_name ~= nil then
        buildings = surface.find_entities_filtered{
            area = area,
            name = source_name
        }
    else
        buildings = surface.find_entities_filtered{
            area = area
        }
    end

    -- Find the closest building with the item we want
    local closest_distance = math.huge
    local closest_entity = nil
    local found_any_items = false

    for _, building in ipairs(buildings) do
        if building.name ~= 'character' then
            local item_count = get_entity_item_count(building, extract_item)
            if item_count > 0 then
                found_any_items = true
                local distance = ((position.x - building.position.x) ^ 2 +
                                (position.y - building.position.y) ^ 2) ^ 0.5
                if distance < closest_distance then
                    closest_distance = distance
                    closest_entity = building
                end
            end
        end
    end

    -- Error handling in priority order

    if #buildings == 0 then
        error("\"Could not find any entities in range\"")
    end

    if not closest_entity then
        if source_name then
            error("\"Could not find a valid "..source_name.." entity containing " .. extract_item.."\"")
        else
            error("\"Could not find a valid entity containing " .. extract_item .. "\"")
        end
    end

    if closest_distance > search_radius then
        error("\"Entity at ("..closest_entity.position.x..", "..closest_entity.position.y..") is too far away from your position of ("..player.position.x..","..player.position.y.."), move closer.\"")
    end

    if not found_any_items then
        error("\"No " .. extract_item .. " found in any nearby entities\"")
    end

    -- Calculate how many items we can actually extract
    local available_count = get_entity_item_count(closest_entity, extract_item)
    local extract_count = math.min(count, available_count)

    -- Create the stack for extraction
    local stack = {name=extract_item, count=extract_count}

    -- Attempt the extraction
    local number_extracted = remove_items_from_entity(closest_entity, stack)

    if number_extracted > 0 then
        -- Insert items into player inventory
        local inserted = player.insert(stack)

        -- If we couldn't insert all items, put them back in the container
        if inserted < number_extracted then
            stack.count = number_extracted - inserted
            closest_entity.insert(stack)
            number_extracted = inserted
        end

        -- game.print("Extracted " .. number_extracted .. " " .. extract_item)
        return number_extracted
    else
        -- This should rarely happen given our prior checks
        error("\"Failed to extract " .. extract_item .. "\"")
    end
end