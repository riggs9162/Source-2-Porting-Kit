"""Generate self-contained GMod drivers for authored model defaults and deformation."""

from __future__ import annotations

import json
from pathlib import Path


_DRIVER = r'''if ( SERVER ) then
    AddCSLuaFile()
end

__CONFIG_LOADER__
local hookId = "S2Kit.Model." .. util.CRC(config.model)
local watched = setmetatable({}, {__mode = "k"})
local previous

local function ReadModelMorphs()
    if ( config.morphIndices ) then
        return config.morphIndices
    end

    config.morphIndices = {}
    local bytes = file.Read(config.model, "GAME")

    if ( !bytes or string.sub(bytes, 1, 4) != "IDST" ) then
        return config.morphIndices
    end

    local function ReadInteger(offset)
        local a, b, c, d = string.byte(bytes, offset + 1, offset + 4)

        if ( !d ) then
            return 0
        end

        return a + b * 256 + c * 65536 + d * 16777216
    end

    local count, offset = ReadInteger(260), ReadInteger(264)

    if ( count > 4096 or offset + count * 4 > #bytes ) then
        return config.morphIndices
    end

    for index = 0, count - 1 do
        local record = offset + index * 4
        local relative = ReadInteger(record)

        if ( relative >= 2147483648 ) then
            relative = relative - 4294967296
        end

        local start = record + relative + 1
        local finish = start >= 1 and string.find(bytes, "\0", start, true)

        if ( finish ) then
            config.morphIndices[string.sub(bytes, start, finish - 1)] = index
        end
    end

    return config.morphIndices
end

local function SampleMorphCurve(curve, time)
    local times, values = curve.times, curve.values
    local count = #times
    local bCubic = curve.interpolation == "CUBICSPLINE"

    if ( count == 1 or time <= times[1] ) then
        return bCubic and values[1][2] or values[1]
    end

    if ( time >= times[count] ) then
        return bCubic and values[count][2] or values[count]
    end

    local lower, upper = 1, count

    while ( upper - lower > 1 ) do
        local middle = math.floor((lower + upper) * 0.5)

        if ( times[middle] <= time ) then
            lower = middle
        else
            upper = middle
        end
    end

    if ( curve.interpolation == "STEP" ) then
        return values[lower]
    end

    local interval = times[upper] - times[lower]
    local amount = (time - times[lower]) / interval

    if ( !bCubic ) then
        return values[lower] * (1 - amount) + values[upper] * amount
    end

    local square, cube = amount * amount, amount * amount * amount

    return (2 * cube - 3 * square + 1) * values[lower][2]
        + (cube - 2 * square + amount) * interval * values[lower][3]
        + (-2 * cube + 3 * square) * values[upper][2]
        + (cube - square) * interval * values[upper][1]
end

local function BuildModelFlexes(entity, count, weights)
    local state = watched[entity]

    if ( !state or string.lower(entity:GetModel() or "") != config.model ) then
        return weights
    end

    local indices = ReadModelMorphs()
    local name = entity:GetSequenceName(entity:GetSequence())
    local clip = (config.morph_animations or {})[name]

    if ( clip ) then
        local time = math.Clamp(entity:GetCycle(), 0, 1) * clip.duration

        for _, target in ipairs(clip.targets) do
            local index = indices[target]

            if ( index and index < count ) then
                local curve = clip.curves[target]
                weights[index] = curve and SampleMorphCurve(curve, time) or 0
            end
        end
    end

    for target, value in pairs(state.skinMorphWeights or {}) do
        local index = indices[target]

        if ( index and index < count ) then
            weights[index] = value
        end
    end

    return weights
end

local function MatrixFromRows(rows)
    local value = Matrix()

    for row = 1, 4 do
        for column = 1, 4 do
            value:SetField(row, column, rows[row][column])
        end
    end

    return value
end

local function SourceModelBasis(entity)
    if ( !config.sourceModelRotation ) then
        config.sourceModelRotation = config.source_model_rotation and MatrixFromRows(config.source_model_rotation) or Matrix()
    end

    if ( !config.compilerBasis ) then
        config.compilerBasis = Matrix()
        config.compilerBasis:SetAngles(Angle(0, 90, 0))
    end

    local model = Matrix()
    model:SetAngles(entity:GetAngles())

    return model * config.compilerBasis * config.sourceModelRotation
end

local function QuaternionMatrix(value)
    local x, y, z, w = value[1], value[2], value[3], value[4]
    local length = math.sqrt(x * x + y * y + z * z + w * w)

    if ( length < 0.00000001 ) then
        return Matrix()
    end

    x, y, z, w = x / length, y / length, z / length, w / length

    return MatrixFromRows({
        {1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w), 0},
        {2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w), 0},
        {2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y), 0},
        {0, 0, 0, 1},
    })
end

local function MatrixQuaternion(matrix)
    local m00, m11, m22 = matrix:GetField(1, 1), matrix:GetField(2, 2), matrix:GetField(3, 3)
    local trace = m00 + m11 + m22
    local x, y, z, w

    if ( trace > 0 ) then
        local scale = math.sqrt(trace + 1) * 2
        x = (matrix:GetField(3, 2) - matrix:GetField(2, 3)) / scale
        y = (matrix:GetField(1, 3) - matrix:GetField(3, 1)) / scale
        z = (matrix:GetField(2, 1) - matrix:GetField(1, 2)) / scale
        w = scale * 0.25
    elseif ( m00 > m11 and m00 > m22 ) then
        local scale = math.sqrt(1 + m00 - m11 - m22) * 2
        x = scale * 0.25
        y = (matrix:GetField(1, 2) + matrix:GetField(2, 1)) / scale
        z = (matrix:GetField(1, 3) + matrix:GetField(3, 1)) / scale
        w = (matrix:GetField(3, 2) - matrix:GetField(2, 3)) / scale
    elseif ( m11 > m22 ) then
        local scale = math.sqrt(1 + m11 - m00 - m22) * 2
        x = (matrix:GetField(1, 2) + matrix:GetField(2, 1)) / scale
        y = scale * 0.25
        z = (matrix:GetField(2, 3) + matrix:GetField(3, 2)) / scale
        w = (matrix:GetField(1, 3) - matrix:GetField(3, 1)) / scale
    else
        local scale = math.sqrt(1 + m22 - m00 - m11) * 2
        x = (matrix:GetField(1, 3) + matrix:GetField(3, 1)) / scale
        y = (matrix:GetField(2, 3) + matrix:GetField(3, 2)) / scale
        z = scale * 0.25
        w = (matrix:GetField(2, 1) - matrix:GetField(1, 2)) / scale
    end

    if ( w < 0 ) then
        return {-x, -y, -z, -w}
    end

    return {x, y, z, w}
end

local function SourceBoneMatrix(entity, name)
    local frame = config.source_frames[name]

    if ( !frame ) then
        return nil
    end

    local index = entity:LookupBone(frame.target)
    local matrix = index and entity:GetBoneMatrix(index)

    if ( !matrix ) then
        return nil
    end

    frame.targetToSource = frame.targetToSource or MatrixFromRows(frame.target_to_source)

    return matrix * frame.targetToSource
end

local function SetBoneWorld(entity, index, replacement, state)
    local current = entity:GetBoneMatrix(index)

    if ( !current ) then
        return
    end

    local inverse = current:GetInverse()

    if ( !inverse ) then
        return
    end

    local delta = replacement * inverse
    local children = state.children[index] or {}
    local pending = {}

    for _, child in ipairs(children) do
        pending[#pending + 1] = child
    end

    entity:SetBoneMatrix(index, replacement)

    local cursor = 1

    while ( cursor <= #pending ) do
        local child = pending[cursor]
        local matrix = entity:GetBoneMatrix(child)

        if ( matrix ) then
            entity:SetBoneMatrix(child, delta * matrix)
        end

        for _, descendant in ipairs(state.children[child] or {}) do
            pending[#pending + 1] = descendant
        end

        cursor = cursor + 1
    end
end

local function ApplyTwists(entity, state, records)
    for _, constraint in ipairs(records or config.twist_constraints or {}) do
        local target = SourceBoneMatrix(entity, constraint.orig_target_bone)
        local parent = SourceBoneMatrix(entity, constraint.orig_parent_bone)
        local slaveFrame = config.source_frames[constraint.slave]
        local slaveParent = SourceBoneMatrix(entity, constraint.slave_parent)
        local slave = slaveFrame and entity:LookupBone(slaveFrame.target)

        if ( target and parent and slaveParent and slave ) then
            local parentInverse = parent:GetInverse()
            constraint.offsetInverse = constraint.offsetInverse or QuaternionMatrix(constraint.offset_quaternion):GetInverse()
            local offsetInverse = constraint.offsetInverse

            if ( parentInverse and offsetInverse ) then
                local relative = offsetInverse * parentInverse * target
                local quaternion = MatrixQuaternion(relative)
                local axis = constraint.target_axis + 1
                local angle = 2 * math.atan2(quaternion[axis], quaternion[4]) * constraint.share
                local value = {0, 0, 0, math.cos(angle * 0.5)}
                value[constraint.slave_axis + 1] = math.sin(angle * 0.5)
                local localMatrix = QuaternionMatrix(value)
                local bind = constraint.original_bind_local

                if ( bind and bind.position ) then
                    localMatrix:SetTranslation(Vector(unpack(bind.position)))
                elseif ( bind and bind[1] and istable(bind[1]) ) then
                    localMatrix:SetTranslation(Vector(bind[1][4], bind[2][4], bind[3][4]))
                end

                slaveFrame.sourceToTarget = slaveFrame.sourceToTarget or MatrixFromRows(slaveFrame.source_to_target)
                local replacement = slaveParent * localMatrix * slaveFrame.sourceToTarget
                SetBoneWorld(entity, slave, replacement, state)
            end
        end
    end
end

local function RadialKernel(radius, kernel, falloff)
    local scaled = falloff > 0 and radius / falloff or radius

    if ( kernel == 1 ) then
        return 1 / math.sqrt(1 + scaled * scaled)
    elseif ( kernel == 2 ) then
        return math.exp(-scaled * scaled)
    elseif ( kernel == 3 ) then
        return radius
    elseif ( kernel == 4 ) then
        return radius ^ 3
    elseif ( kernel == 5 ) then
        return radius ^ 5
    elseif ( kernel == 6 ) then
        return radius * radius * math.log(radius)
    end

    return math.sqrt(1 + scaled * scaled)
end

local function RadialWeights(basis, point)
    local distances = {}
    local weights = {}

    for index, sample in ipairs(basis.samples) do
        local x, y, z = point.x - sample[1], point.y - sample[2], point.z - sample[3]
        distances[index] = RadialKernel(math.sqrt(x * x + y * y + z * z), basis.kernel, basis.falloff)
    end

    for index, row in ipairs(basis.inverse) do
        local value = 0

        for sample, coefficient in ipairs(row) do
            value = value + coefficient * distances[sample]
        end

        weights[index] = value
    end

    return weights
end

local function QuaternionSlerp(first, second, amount)
    local dot = 0
    local target = {}

    for index = 1, 4 do
        dot = dot + first[index] * second[index]
        target[index] = second[index]
    end

    if ( dot < 0 ) then
        dot = -dot

        for index = 1, 4 do
            target[index] = -target[index]
        end
    end

    dot = math.Clamp(dot, -1, 1)
    local left, right = 1 - amount, amount

    if ( dot < 0.9995 ) then
        local angle = math.acos(dot)
        local sine = math.sin(angle)
        left = math.sin((1 - amount) * angle) / sine
        right = math.sin(amount * angle) / sine
    end

    local result = {}

    for index = 1, 4 do
        result[index] = first[index] * left + target[index] * right
    end

    return result
end

local function WeightedQuaternion(rotations, weights)
    if ( #rotations == 1 ) then
        return rotations[1]
    end

    local total = 0
    local sum = {0, 0, 0, 0}

    for _, weight in ipairs(weights) do
        total = total + weight
    end

    local inverse = total > 0 and 1 / total or 1

    for index, value in ipairs(rotations) do
        local dot = 0

        for component = 1, 4 do
            dot = dot + rotations[1][component] * value[component]
        end

        local sign = dot < 0 and -1 or 1
        local x, y, z, w = value[1] * sign, value[2] * sign, value[3] * sign, value[4] * sign
        local length = math.sqrt(x * x + y * y + z * z)
        local scale = length >= 0.00001 and math.atan2(length, w) / length or 0
        local weight = weights[index] * inverse
        sum[1] = sum[1] + x * scale * weight
        sum[2] = sum[2] + y * scale * weight
        sum[3] = sum[3] + z * scale * weight
        sum[4] = sum[4] + 0.5 * math.log(x * x + y * y + z * z + w * w) * weight
    end

    local angle = math.sqrt(sum[1] * sum[1] + sum[2] * sum[2] + sum[3] * sum[3])
    local magnitude = math.exp(sum[4])
    local scale = angle >= 0.00001 and math.sin(angle) * magnitude / angle or 0

    return {sum[1] * scale, sum[2] * scale, sum[3] * scale, math.cos(angle) * magnitude}
end

local function QuaternionNlerp(first, second, amount)
    local dot = 0

    for index = 1, 4 do
        dot = dot + first[index] * second[index]
    end

    local sign = dot < 0 and -1 or 1
    local result = {}

    for index = 1, 4 do
        result[index] = first[index] * (1 - amount) + second[index] * sign * amount
    end

    return result
end

local function PosePoint(entity, constraint)
    local bone = SourceBoneMatrix(entity, constraint.bone)
    local parent = SourceBoneMatrix(entity, constraint.parent)
    local inverse = parent and parent:GetInverse()

    if ( !bone or !inverse ) then
        return nil
    end

    return (inverse * bone) * Vector(unpack(constraint.offset))
end

local function SetSourceWorld(entity, name, replacement, state)
    local frame = config.source_frames[name]
    local index = frame and entity:LookupBone(frame.target)

    if ( index ) then
        frame.sourceToTarget = frame.sourceToTarget or MatrixFromRows(frame.source_to_target)
        SetBoneWorld(entity, index, replacement * frame.sourceToTarget, state)
    end
end

local function FlexIndex(entity, state, name)
    if ( !state.flexes ) then
        state.flexes = {}

        for index = 0, entity:GetFlexNum() - 1 do
            state.flexes[string.lower(entity:GetFlexName(index))] = index
        end
    end

    return state.flexes[string.lower(name)]
end

local function ApplyPoseMorph(entity, constraint, state)
    local point = PosePoint(entity, constraint)

    if ( !point ) then
        return
    end

    local weights = RadialWeights(constraint.basis, point)

    for output, name in ipairs(constraint.outputs) do
        local value = 0

        for sample, weight in ipairs(weights) do
            value = value + weight * constraint.output_weights[sample][output]
        end

        if ( constraint.clamp ) then
            value = math.Clamp(value, 0, 1)
        end

        local index = FlexIndex(entity, state, name)

        if ( index ) then
            entity:SetFlexWeight(index, value)
        end
    end
end

local function ApplyPoseBone(entity, constraint, state)
    local point = PosePoint(entity, constraint)

    if ( !point ) then
        return
    end

    local weights = RadialWeights(constraint.basis, point)

    for output, slave in ipairs(constraint.slaves) do
        local position = Vector()
        local rotations = {}

        for sample, weight in ipairs(weights) do
            local value = constraint.output_transforms[sample][output]
            position = position + Vector(value[1], value[2], value[3]) * weight
            rotations[sample] = {value[4], value[5], value[6], value[7]}
        end

        local replacement = QuaternionMatrix(WeightedQuaternion(rotations, weights))
        replacement:SetTranslation(position)
        local parent = SourceBoneMatrix(entity, slave.parent)

        if ( parent ) then
            replacement = parent * replacement
        end

        SetSourceWorld(entity, slave.bone, replacement, state)
    end
end

local function TargetMatrix(entity, target)
    local bone = SourceBoneMatrix(entity, target.bone)

    if ( !bone ) then
        return nil
    end

    if ( !target.localMatrix ) then
        local attachment = QuaternionMatrix(target.attachment_rotation)
        attachment:SetTranslation(Vector(unpack(target.attachment_position)))
        local offset = QuaternionMatrix(target.rotation_offset)
        offset:SetTranslation(Vector(unpack(target.position_offset)))
        target.localMatrix = attachment * offset
    end

    return bone * target.localMatrix
end

local function ApplyTargetConstraint(entity, constraint, state)
    local matrices, rotations, weights = {}, {}, {}
    local position = Vector()
    local total = 0
    local count = #constraint.targets

    if ( constraint.kind == "aim" and count > 1 ) then
        count = count - 1
    end

    for index, target in ipairs(constraint.targets) do
        local matrix = TargetMatrix(entity, target)

        if ( !matrix ) then
            return
        end

        matrices[index] = matrix
        rotations[index] = MatrixQuaternion(matrix)
        weights[index] = target.weight

        if ( index <= count ) then
            position = position + matrix:GetTranslation() * target.weight
            total = total + target.weight
        end
    end

    if ( total != 0 ) then
        position = position / total
    end

    for _, slave in ipairs(constraint.slaves) do
        local current = SourceBoneMatrix(entity, slave.bone)
        local weight = total * slave.weight

        if ( current and math.abs(weight) > 0.0001 ) then
            local replacement = MatrixFromRows({
                {current:GetField(1, 1), current:GetField(1, 2), current:GetField(1, 3), current:GetField(1, 4)},
                {current:GetField(2, 1), current:GetField(2, 2), current:GetField(2, 3), current:GetField(2, 4)},
                {current:GetField(3, 1), current:GetField(3, 2), current:GetField(3, 3), current:GetField(3, 4)},
                {0, 0, 0, 1},
            })
            local orientation

            if ( constraint.kind == "aim" ) then
                local parent = SourceBoneMatrix(entity, slave.parent) or Matrix()
                local inverse = parent:GetInverse()

                if ( inverse and (constraint.up_type != 0 and constraint.up_type != 2 or #matrices > 1) ) then
                    local up = Vector(unpack(constraint.up_vector))
                    local upTarget = matrices[#matrices]
                    local origin = current:GetTranslation()

                    if ( constraint.up_type == 2 ) then
                        up = (upTarget:GetTranslation() - origin):GetNormalized()
                    elseif ( constraint.up_type == 0 ) then
                        up = upTarget:GetAngles():Forward() * up.x + upTarget:GetAngles():Right() * -up.y + upTarget:GetAngles():Up() * up.z
                    elseif ( constraint.up_type == 3 ) then
                        up = parent:GetAngles():Forward() * up.x + parent:GetAngles():Right() * -up.y + parent:GetAngles():Up() * up.z
                    else
                        up = SourceModelBasis(entity) * up
                    end

                    local localOrigin = inverse * origin
                    local direction = (inverse * position - localOrigin):GetNormalized()
                    local localUp = (inverse * (origin + up) - localOrigin):GetNormalized()
                    localUp = (localUp - direction * localUp:Dot(direction)):GetNormalized()
                    local side = direction:Cross(localUp):GetNormalized()
                    local look = MatrixFromRows({
                        {side.x, direction.x, localUp.x, 0},
                        {side.y, direction.y, localUp.y, 0},
                        {side.z, direction.z, localUp.z, 0},
                        {0, 0, 0, 1},
                    })
                    orientation = MatrixQuaternion(parent * look * QuaternionMatrix(constraint.aim_offset))
                end
            elseif ( constraint.kind == "orient" or constraint.kind == "parent" ) then
                orientation = WeightedQuaternion(rotations, weights)
            end

            if ( orientation ) then
                local amount = constraint.kind == "parent" and (math.abs(weight - 1) <= 0.0001 and 1 or slave.weight) or weight
                local rotation

                if ( constraint.kind == "parent" ) then
                    rotation = QuaternionNlerp(MatrixQuaternion(current), orientation, amount)
                else
                    rotation = QuaternionSlerp(MatrixQuaternion(current), orientation, amount)
                end

                local changed = QuaternionMatrix(rotation)
                changed:SetTranslation(current:GetTranslation())
                replacement = changed
            end

            if ( constraint.kind == "point" or constraint.kind == "parent" ) then
                local amount = constraint.kind == "parent" and (math.abs(weight - 1) <= 0.0001 and 1 or slave.weight) or weight
                replacement:SetTranslation(current:GetTranslation() + (position - current:GetTranslation()) * amount)
            end

            SetSourceWorld(entity, slave.bone, replacement, state)
        end
    end
end

local function ApplyProcedurals(entity, state)
    for _, constraint in ipairs(config.runtime_constraints or {}) do
        if ( constraint.kind == "twist" ) then
            ApplyTwists(entity, state, {constraint})
        elseif ( constraint.kind == "pose_morph" ) then
            ApplyPoseMorph(entity, constraint, state)
        elseif ( constraint.kind == "pose_bone" ) then
            ApplyPoseBone(entity, constraint, state)
        else
            ApplyTargetConstraint(entity, constraint, state)
        end
    end
end

local function ApplySkinMorphs(entity, state)
    local skin = entity:GetSkin()

    if ( state.skin == skin ) then
        return
    end

    local name = config.skin_names[skin + 1]
    local weights = {}

    for _, rule in ipairs(config.skin_morphs or {}) do
        weights[rule.morph_name] = math.max(weights[rule.morph_name] or 0, rule.materialgroup_name == name and 1 or 0)
    end

    for morph, weight in pairs(weights) do
        local index = entity:GetFlexIDByName(morph)

        if ( index ) then
            entity:SetFlexWeight(index, weight)
        end
    end

    state.skinMorphWeights = weights
    state.skin = skin
end

local function BuildModelBones(entity)
    local state = watched[entity]

    if ( !state or string.lower(entity:GetModel() or "") != config.model ) then
        return
    end

    if ( !state.children ) then
        state.children = {}

        for index = 0, entity:GetBoneCount() - 1 do
            local parent = entity:GetBoneParent(index)
            state.children[parent] = state.children[parent] or {}
            table.insert(state.children[parent], index)
        end
    end

    ApplyProcedurals(entity, state)
end

local function InitializeModel(entity)
    if ( !IsValid(entity) or string.lower(entity:GetModel() or "") != config.model ) then
        return
    end

    if ( watched[entity] ) then
        return
    end

    local state = {}
    watched[entity] = state

    if ( !previous or !previous.watched[entity] ) then
        for name, choice in pairs(config.bodygroup_defaults or {}) do
            local index = entity:FindBodygroupByName(name)

            if ( index >= 0 ) then
                entity:SetBodygroup(index, choice)
            end
        end
    end

    if ( CLIENT ) then
        state.callbackId = entity:AddCallback("BuildBonePositions", BuildModelBones)
        state.flexCallbackId = entity:AddCallback("BuildFlexWeights", BuildModelFlexes)
        ApplySkinMorphs(entity, state)
    end
end

local function QueueModel(entity)
    timer.Simple(0, function()
        InitializeModel(entity)
    end)

    timer.Simple(0.25, function()
        InitializeModel(entity)
    end)
end

local function UpdateSkinMorphs()
    for entity, state in pairs(watched) do
        if ( IsValid(entity) and string.lower(entity:GetModel() or "") == config.model ) then
            ApplySkinMorphs(entity, state)
        else
            if ( IsValid(entity) ) then
                if ( state.callbackId ) then
                    entity:RemoveCallback("BuildBonePositions", state.callbackId)
                end

                if ( state.flexCallbackId ) then
                    entity:RemoveCallback("BuildFlexWeights", state.flexCallbackId)
                end
            end

            watched[entity] = nil
        end
    end

    for _, client in player.Iterator() do
        InitializeModel(client)
    end
end

_G.S2KitModelDrivers = _G.S2KitModelDrivers or {}
previous = _G.S2KitModelDrivers[hookId]

if ( previous ) then
    for entity, state in pairs(previous.watched) do
        if ( IsValid(entity) and state.callbackId ) then
            entity:RemoveCallback("BuildBonePositions", state.callbackId)
        end

        if ( IsValid(entity) and state.flexCallbackId ) then
            entity:RemoveCallback("BuildFlexWeights", state.flexCallbackId)
        end
    end
end

_G.S2KitModelDrivers[hookId] = {watched = watched}

hook.Add("OnEntityCreated", hookId, QueueModel)
hook.Add("PlayerSpawn", hookId, QueueModel)
hook.Add("PlayerSetModel", hookId, QueueModel)

if ( CLIENT ) then
    hook.Add("Think", hookId, UpdateSkinMorphs)
end

for _, entity in ents.Iterator() do
    InitializeModel(entity)
end
'''


def write_model_driver(output: Path, modelname: str, data: dict) -> Path:
    """Write a shared autorun driver with model-scoped state and no external dependency."""
    from s2kit.pipelines.ragdoll_correctives import prepare_corrective_drivers

    configuration = dict(data, model=("models/" + modelname).lower())
    configuration.update(prepare_corrective_drivers(configuration))
    configuration.setdefault("source_frames", {})
    configuration.setdefault("skin_names", [])
    configuration.pop("source_constraints", None)
    encoded = json.dumps(configuration, separators=(",", ":"))
    if "]==]" in encoded:
        raise ValueError("Model driver data contains a reserved Lua delimiter")
    import hashlib

    output = Path(output)
    path = output / "model_driver.lua"
    identity = hashlib.sha256(encoded.encode()).hexdigest()[:16]
    folder = output / "driver_data" / identity
    folder.mkdir(parents=True, exist_ok=True)
    entries = []
    for index, start in enumerate(range(0, len(encoded), 48000)):
        part = folder / f"config_{index:04d}.lua"
        part.write_text("return [==[" + encoded[start:start + 48000] + "]==]\n", encoding="utf-8")
        entries.append(dict(source=str(part), relative=f"lua/s2kit/{identity}/{part.name}"))
    includes = [entry["relative"].removeprefix("lua/") for entry in entries]
    loader = "if ( SERVER ) then\n" + "".join(f"    AddCSLuaFile({json.dumps(name)})\n" for name in includes) + "end\n\n"
    loader += "local config = util.JSONToTable(table.concat({\n"
    loader += "".join(f"    include({json.dumps(name)}),\n" for name in includes)
    loader += "}))"
    path.write_text(_DRIVER.replace("__CONFIG_LOADER__", loader), encoding="utf-8", newline="\n")
    manifest = path.with_suffix(".payload.json")
    manifest.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
    return path


def model_driver_files(path: Path) -> list[Path]:
    """List generated configuration chunks alongside the shared driver and its manifest."""
    path = Path(path)
    manifest = path.with_suffix(".payload.json")
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    return [path, manifest, *(Path(entry["source"]) for entry in entries)]
