// Ported from AMD Compressonator's DXTC codec (cmp_compressonatorlib/ati/compressonatorxcodec.cpp).
// Copyright (c) 2007-2024 Advanced Micro Devices, Inc. All rights reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
// associated documentation files (the "Software"), to deal in the Software without restriction,
// including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense,
// and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
// subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all copies or substantial
// portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
// LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
// NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
// WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
// SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

using System.Buffers.Binary;

namespace PortingKit.Addons.Porting;

/// <summary>Compresses one 4x4 block with the same encoder the kit's VTF output has always come from.</summary>
/// <remarks>
/// <para>This reproduces the original single-precision arithmetic step for step, and each expression keeps its original order of
/// evaluation. Identical blocks are not a nicety: whether an opaque texture is stored as DXT1 depends on this encoder recovering
/// a DXT5 block's decoded pixels exactly, so a different encoder of equal quality changes the file format of about one texture in
/// ten. One instance holds one worker's scratch space, so a block fit never allocates.</para>
/// <para>One thing cannot be reproduced: with one-bit alpha the original sorts fifteen entries whatever the number of opaque pixels, so
/// a block mixing transparent and opaque pixels sorts stack garbage. Here only the real pixels are sorted, which can change such a
/// block's endpoints but not the opaque or DXT5 paths.</para>
/// <para>Three of the original's quirks are load-bearing and kept. DXT5 and DXT3 colour goes through the same three-or-four colour
/// choice as DXT1, so a DXT5 block can be fitted for three colours though the decoder always uses four. Only the first fifteen
/// pixels are sorted before duplicates are merged, so the last pixel can appear twice. And the endpoint search never resets its
/// high bound between low steps, so it walks further down on each pass.</para>
/// </remarks>
internal sealed class BlockEncoder
{
    private const int Pixels = 16;
    private const int RedBits = 5, GreenBits = 6, BlueBits = 5;
    private const int Red = 2, Green = 1, Blue = 0;
    private const float MaxError = 128000f;
    private const float SearchStep = 0.025f;
    private const float GlobalStep = 0.018f, GlobalExtent = 0.1f, LocalStep = 0.6f;
    private const int MoveSteps = 3;
    private static readonly float[] Moves = [0f, -1f, 1f];
    private static readonly int[] RoundAmount = [0, 0, 0, 0, 1, 1, 2, 2, 3];
    private static readonly float[] GridFactors = BuildFactors(RedBits, GreenBits, BlueBits);
    private static readonly float[] CellFactors = BuildFactors(8 - RedBits, 8 - GreenBits, 8 - BlueBits);

    /// <summary>Endpoint pairs for a solid 5-bit channel: two values per 8-bit level, the first going to the lower endpoint.</summary>
    private static readonly byte[] SolidFive =
    [
        0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 1, 1, 1, 1, 1, 1, 0, 2, 4, 0, 1, 2, 1, 2, 1, 2, 2, 2, 2, 2, 2, 2, 1, 3, 5, 1,
        2, 3, 2, 3, 0, 4, 3, 3, 3, 3, 3, 3, 2, 4, 2, 4, 2, 4, 5, 3, 1, 5, 1, 5, 2, 5, 4, 4, 4, 4, 3, 5, 3, 5, 2, 6, 2, 6, 2, 6,
        3, 6, 5, 5, 5, 5, 4, 6, 8, 4, 3, 7, 3, 7, 3, 7, 6, 6, 6, 6, 6, 6, 5, 7, 9, 5, 6, 7, 6, 7, 4, 8, 7, 7, 7, 7, 7, 7, 6, 8,
        6, 8, 6, 8, 9, 7, 5, 9, 5, 9, 6, 9, 8, 8, 8, 8, 7, 9, 7, 9, 6, 10, 6, 10, 6, 10, 7, 10, 9, 9, 9, 9, 8, 10, 12, 8, 7, 11, 7, 11,
        7, 11, 10, 10, 10, 10, 10, 10, 9, 11, 13, 9, 10, 11, 10, 11, 8, 12, 11, 11, 11, 11, 11, 11, 10, 12, 10, 12, 10, 12, 13, 11, 9, 13, 9, 13, 10, 13, 12, 12,
        12, 12, 11, 13, 11, 13, 10, 14, 10, 14, 10, 14, 11, 14, 13, 13, 13, 13, 12, 14, 16, 12, 11, 15, 11, 15, 11, 15, 14, 14, 14, 14, 14, 14, 13, 15, 17, 13, 14, 15,
        14, 15, 12, 16, 15, 15, 15, 15, 15, 15, 14, 16, 14, 16, 14, 16, 17, 15, 13, 17, 13, 17, 14, 17, 16, 16, 16, 16, 15, 17, 15, 17, 14, 18, 14, 18, 14, 18, 15, 18,
        17, 17, 17, 17, 16, 18, 20, 16, 15, 19, 15, 19, 15, 19, 18, 18, 18, 18, 18, 18, 17, 19, 21, 17, 18, 19, 18, 19, 16, 20, 19, 19, 19, 19, 19, 19, 18, 20, 18, 20,
        18, 20, 21, 19, 17, 21, 17, 21, 18, 21, 20, 20, 20, 20, 19, 21, 19, 21, 18, 22, 18, 22, 18, 22, 19, 22, 21, 21, 21, 21, 20, 22, 24, 20, 19, 23, 19, 23, 19, 23,
        22, 22, 22, 22, 22, 22, 21, 23, 25, 21, 22, 23, 22, 23, 20, 24, 23, 23, 23, 23, 23, 23, 22, 24, 22, 24, 22, 24, 25, 23, 21, 25, 21, 25, 22, 25, 24, 24, 24, 24,
        23, 25, 23, 25, 22, 26, 22, 26, 22, 26, 23, 26, 25, 25, 25, 25, 24, 26, 28, 24, 23, 27, 23, 27, 23, 27, 26, 26, 26, 26, 26, 26, 25, 27, 29, 25, 26, 27, 26, 27,
        24, 28, 27, 27, 27, 27, 27, 27, 26, 28, 26, 28, 26, 28, 29, 27, 25, 29, 25, 29, 26, 29, 28, 28, 28, 28, 27, 29, 27, 29, 26, 30, 26, 30, 26, 30, 27, 30, 29, 29,
        29, 29, 28, 30, 28, 30, 27, 31, 27, 31, 27, 31, 30, 30, 30, 30, 30, 30, 29, 31, 29, 31, 30, 31, 30, 31, 30, 31, 31, 31, 31, 31
    ];

    /// <summary>Endpoint pairs for a solid 6-bit channel: two values per 8-bit level, the first going to the lower endpoint.</summary>
    private static readonly byte[] SolidSix =
    [
        0, 0, 1, 0, 0, 1, 1, 1, 1, 1, 0, 2, 1, 2, 2, 2, 2, 2, 1, 3, 0, 4, 3, 3, 3, 3, 0, 5, 1, 5, 4, 4, 4, 4, 1, 6, 0, 7, 5, 5,
        5, 5, 0, 8, 1, 8, 6, 6, 6, 6, 1, 9, 2, 9, 7, 7, 7, 7, 2, 10, 3, 10, 8, 8, 8, 8, 3, 11, 4, 11, 9, 9, 9, 9, 4, 12, 5, 12, 10, 10,
        10, 10, 5, 13, 6, 13, 16, 8, 11, 11, 6, 14, 7, 14, 17, 9, 12, 12, 7, 15, 8, 15, 16, 11, 13, 13, 10, 15, 8, 16, 9, 16, 14, 14, 13, 15, 9, 17, 10, 17,
        15, 15, 16, 15, 10, 18, 11, 18, 12, 18, 16, 16, 11, 19, 12, 19, 13, 19, 17, 17, 12, 20, 13, 20, 14, 20, 18, 18, 13, 21, 14, 21, 15, 21, 19, 19, 14, 22, 15, 22,
        20, 20, 20, 20, 15, 23, 16, 23, 21, 21, 21, 21, 16, 24, 17, 24, 22, 22, 22, 22, 17, 25, 18, 25, 23, 23, 23, 23, 18, 26, 19, 26, 24, 24, 24, 24, 19, 27, 20, 27,
        25, 25, 25, 25, 20, 28, 21, 28, 26, 26, 26, 26, 21, 29, 22, 29, 32, 24, 27, 27, 22, 30, 23, 30, 33, 25, 28, 28, 23, 31, 24, 31, 32, 27, 29, 29, 26, 31, 24, 32,
        25, 32, 30, 30, 29, 31, 25, 33, 26, 33, 31, 31, 32, 31, 26, 34, 27, 34, 28, 34, 32, 32, 27, 35, 28, 35, 29, 35, 33, 33, 28, 36, 29, 36, 30, 36, 34, 34, 29, 37,
        30, 37, 31, 37, 35, 35, 30, 38, 31, 38, 36, 36, 36, 36, 31, 39, 32, 39, 37, 37, 37, 37, 32, 40, 33, 40, 38, 38, 38, 38, 33, 41, 34, 41, 39, 39, 39, 39, 34, 42,
        35, 42, 40, 40, 40, 40, 35, 43, 36, 43, 41, 41, 41, 41, 36, 44, 37, 44, 42, 42, 42, 42, 37, 45, 38, 45, 48, 40, 43, 43, 38, 46, 39, 46, 49, 41, 44, 44, 39, 47,
        40, 47, 48, 43, 45, 45, 42, 47, 40, 48, 41, 48, 46, 46, 45, 47, 41, 49, 42, 49, 47, 47, 48, 47, 42, 50, 43, 50, 44, 50, 48, 48, 43, 51, 44, 51, 45, 51, 49, 49,
        44, 52, 45, 52, 46, 52, 50, 50, 45, 53, 46, 53, 47, 53, 51, 51, 46, 54, 47, 54, 52, 52, 52, 52, 47, 55, 48, 55, 53, 53, 53, 53, 48, 56, 49, 56, 54, 54, 54, 54,
        49, 57, 50, 57, 55, 55, 55, 55, 50, 58, 51, 58, 56, 56, 56, 56, 51, 59, 52, 59, 57, 57, 57, 57, 52, 60, 53, 60, 58, 58, 58, 58, 53, 61, 54, 61, 59, 59, 59, 59,
        54, 62, 55, 62, 60, 60, 60, 60, 55, 63, 56, 63, 61, 61, 61, 61, 58, 63, 59, 63, 62, 62, 62, 62, 61, 63, 62, 63, 63, 63, 63, 63
    ];

    private readonly uint[] block = new uint[Pixels];
    private readonly uint[] sorted = new uint[Pixels];
    private readonly uint[] unique = new uint[Pixels];
    private readonly float[] repeats = new float[Pixels];
    private readonly float[,] source = new float[Pixels, 3];
    private readonly float[,] unit = new float[Pixels, 3];
    private readonly float[,] shifted = new float[Pixels, 3];
    private readonly float[] projected0 = new float[Pixels], projected = new float[Pixels], projectionError = new float[Pixels];
    private readonly float[] weighted = new float[Pixels], rampIndices = new float[Pixels];
    private readonly float[,] ramp = new float[3, 16];
    private readonly float[,] rampError = new float[16, Pixels];
    private readonly float[,] working = new float[3, 2];
    private readonly float[,] current = new float[3, 2];
    private readonly float[,] original = new float[3, 2];
    private readonly float[,] onGrid = new float[3, 2];
    private readonly float[,] result = new float[3, 2];
    private readonly float[,] raw = new float[3, 2];
    private readonly float[,] pixelColours = new float[Pixels, 3];
    private readonly bool[] opaque = new bool[Pixels];
    private readonly byte[,] endpoints3 = new byte[3, 2], endpoints4 = new byte[3, 2];
    private readonly byte[] indices3 = new byte[Pixels], indices4 = new byte[Pixels];
    private readonly byte[] alphaBytes = new byte[Pixels];
    private readonly float[] alphaValues = new float[Pixels], alphaSorted = new float[Pixels];
    private readonly float[] alphaUnique = new float[Pixels], alphaRepeats = new float[Pixels];
    private readonly byte[] alphaIndices8 = new byte[Pixels], alphaIndices6 = new byte[Pixels];
    private readonly float[] alphaRamp = new float[16];
    private readonly float[,] saturated = new float[Pixels, 3];
    private readonly float[] axisPositions = new float[Pixels];
    private uint fallback0, fallback1;

    private static float[] BuildFactors(int red, int green, int blue)
    {
        var factors = new float[3];
        factors[Red] = 1 << red;
        factors[Green] = 1 << green;
        factors[Blue] = 1 << blue;
        return factors;
    }

    /// <summary>Loads a block of tightly packed RGBA pixels from an image whose dimensions are multiples of four.</summary>
    public void Load(ReadOnlySpan<byte> rgba, int stride, int left, int top)
    {
        for (var row = 0; row < 4; row++)
            for (var column = 0; column < 4; column++)
            {
                var from = (top + row) * stride + (left + column) * 4;
                block[row * 4 + column] = (uint)(rgba[from + 3] << 24 | rgba[from] << 16 | rgba[from + 1] << 8 | rgba[from + 2]);
                alphaBytes[row * 4 + column] = rgba[from + 3];
            }
    }

    /// <summary>Writes the colour block, or a one-bit alpha colour block when <paramref name="useAlpha"/> is set.</summary>
    /// <remarks>Solid blocks use lookup tables that land each channel on an exact interpolant, which beats any endpoint pair at 565 precision.</remarks>
    public void Colour(Span<byte> target, bool useAlpha)
    {
        if (!useAlpha && Solid(target)) return;
        var threshold = useAlpha ? 0x80000000u : 0u;
        var error3 = CompressRgb(endpoints3, indices3, 3, useAlpha, threshold);
        if (fallback0 > 0)
        {
            WriteWords(target, fallback0, fallback1);
            return;
        }
        var error4 = error3 == 0f ? float.MaxValue : CompressRgb(endpoints4, indices4, 4, useAlpha, threshold);
        var three = error3 <= error4;
        var endpoints = three ? endpoints3 : endpoints4;
        var indices = three ? indices3 : indices4;
        var c0 = Colour(endpoints, 0);
        var c1 = Colour(endpoints, 1);
        var swap = (!three && c0 <= c1) || (three && c0 > c1);
        WriteColour(target, swap ? c1 : c0, swap ? c0 : c1, indices);
    }

    private bool Solid(Span<byte> target)
    {
        for (var i = 1; i < Pixels; i++)
            if (((block[i] ^ block[0]) & 0xFFFFFF) != 0) return false;
        var red = (int)((block[0] >> 16) & 0xFF);
        var green = (int)((block[0] >> 8) & 0xFF);
        var blue = (int)(block[0] & 0xFF);
        var mask = 0xAAAAAAAAu;
        var low = (uint)(SolidFive[red * 2] << 11 | SolidSix[green * 2] << 5 | SolidFive[blue * 2]);
        var high = (uint)(SolidFive[red * 2 + 1] << 11 | SolidSix[green * 2 + 1] << 5 | SolidFive[blue * 2 + 1]);
        if (high < low)
        {
            (low, high) = (high, low);
            mask ^= 0x55555555u;
        }
        WriteWords(target, high | low << 16, mask);
        return true;
    }

    private static void WriteWords(Span<byte> target, uint first, uint second)
    {
        BinaryPrimitives.WriteUInt32LittleEndian(target, first);
        BinaryPrimitives.WriteUInt32LittleEndian(target[4..], second);
    }

    /// <summary>Writes the block in DXT5: an interpolated alpha block, then a colour block.</summary>
    public void Dxt5(Span<byte> target)
    {
        Alpha(target);
        Colour(target[8..], false);
    }

    /// <summary>Writes the block in DXT3: four explicit alpha bits per pixel, then a four-colour block.</summary>
    public void Dxt3(Span<byte> target)
    {
        var bits = 0UL;
        for (var i = 0; i < Pixels; i++)
        {
            int value = alphaBytes[i];
            value = (value + 7 - (value >> 4)) >> 4;
            bits |= (ulong)Math.Clamp(value, 0, 15) << (4 * i);
        }
        for (var i = 0; i < 8; i++) target[i] = (byte)(bits >> (8 * i));
        Colour(target[8..], false);
    }

    private static uint Colour(byte[,] endpoints, int which) =>
        (uint)((endpoints[Red, which] >> (8 - RedBits)) << 11 | (endpoints[Green, which] >> (8 - GreenBits)) << 5 | endpoints[Blue, which] >> (8 - BlueBits));

    private static void WriteColour(Span<byte> target, uint first, uint second, byte[] indices)
    {
        target[0] = (byte)first;
        target[1] = (byte)(first >> 8);
        target[2] = (byte)second;
        target[3] = (byte)(second >> 8);
        var packed = 0u;
        for (var i = 0; i < Pixels; i++) packed |= (uint)indices[i] << (2 * i);
        target[4] = (byte)packed;
        target[5] = (byte)(packed >> 8);
        target[6] = (byte)(packed >> 16);
        target[7] = (byte)(packed >> 24);
    }

    /// <summary>Fits one colour mode and returns its squared error.</summary>
    private float CompressRgb(byte[,] endpoints, byte[] indices, int points, bool useAlpha, uint threshold)
    {
        Array.Clear(repeats);
        fallback0 = 0;
        fallback1 = 0;
        var colours = 0;
        for (var i = 0; i < Pixels; i++)
            if (!useAlpha || (block[i] & 0xFF000000) >= threshold) sorted[colours++] = block[i] | 0xFF000000;
        if (colours == 0)
        {
            endpoints[0, 0] = endpoints[1, 0] = endpoints[2, 0] = 0;
            endpoints[0, 1] = endpoints[1, 1] = endpoints[2, 1] = 0xFF;
            Array.Fill(indices, (byte)3);
            return 0f;
        }
        if (colours != Pixels && useAlpha && (points & 1) == 0) return float.MaxValue;
        Array.Sort(sorted, 0, Math.Min(colours, Pixels - 1));
        var uniqueColours = 0;
        unique[0] = sorted[0];
        repeats[0] = 1f;
        for (var i = 1; i < colours; i++)
        {
            if (sorted[i] != unique[uniqueColours])
            {
                unique[++uniqueColours] = sorted[i];
                repeats[uniqueColours] = 1f;
            }
            else repeats[uniqueColours] += 1f;
        }
        uniqueColours++;
        for (var i = 0; i < uniqueColours; i++)
        {
            source[i, Red] = (unique[i] >> 16) & 0xFF;
            source[i, Green] = (unique[i] >> 8) & 0xFF;
            source[i, Blue] = unique[i] & 0xFF;
        }
        if (!FitEndpoints(uniqueColours, points))
        {
            if (points >= 4) return float.MaxValue;
            return FastBlock();
        }
        for (var c = 0; c < 3; c++)
            for (var e = 0; e < 2; e++) endpoints[c, e] = (byte)(int)result[c, e];
        return Cluster(endpoints, indices, points, useAlpha, threshold);
    }

    private bool FitEndpoints(int colours, int points)
    {
        for (var i = 0; i < colours; i++)
            for (var c = 0; c < 3; c++) unit[i, c] = source[i, c] / 255f;
        var done = colours <= 2;
        Span<float> mean = stackalloc float[3];
        Span<float> direction = stackalloc float[3];
        if (!done) done = FindAxis(direction, mean, colours);
        if (done)
        {
            for (var c = 0; c < 3; c++)
            {
                raw[c, 0] = source[0, c];
                raw[c, 1] = source[colours - 1, c];
            }
        }
        else if (!SearchAxis(direction, mean, colours, points)) return false;
        OntoGrid();
        Refine(colours, points);
        return true;
    }

    private bool SearchAxis(Span<float> direction, ReadOnlySpan<float> mean, int colours, int points)
    {
        var best = 10000000f;
        Span<float> bestDirection = stackalloc float[3];
        Span<float> bestPosition = stackalloc float[2];
        Span<float> position = stackalloc float[2];
        Span<float> bounds = stackalloc float[2];
        Span<float> cross = stackalloc float[3];
        while (true)
        {
            bounds[0] = 1000f;
            bounds[1] = -1000f;
            Array.Clear(projected0);
            Array.Clear(projected);
            Array.Clear(projectionError);
            Array.Clear(weighted);
            for (var i = 0; i < colours; i++)
            {
                var along = shifted[i, 0] * direction[0] + shifted[i, 1] * direction[1] + shifted[i, 2] * direction[2];
                projected0[i] = projected[i] = along;
                projectionError[i] = (shifted[i, 0] - direction[0] * along) * (shifted[i, 0] - direction[0] * along)
                    + (shifted[i, 1] - direction[1] * along) * (shifted[i, 1] - direction[1] * along)
                    + (shifted[i, 2] - direction[2] * along) * (shifted[i, 2] - direction[2] * along);
                bounds[0] = along < bounds[0] ? along : bounds[0];
                bounds[1] = bounds[1] < along ? along : bounds[1];
            }
            var scaleLow = bounds[0] - (bounds[1] - bounds[0]) * 0.125f;
            var scaleHigh = bounds[1] + (bounds[1] - bounds[0]) * 0.125f;
            if (scaleLow == scaleHigh) return false;
            var scaleSquared = (scaleHigh - scaleLow) * (scaleHigh - scaleLow);
            var overScale = 1f / (scaleHigh - scaleLow);
            for (var i = 0; i < colours; i++)
            {
                projected[i] = (projected[i] - scaleLow) * overScale;
                weighted[i] = repeats[i] * scaleSquared;
            }
            bounds[0] = (bounds[0] - scaleLow) * overScale;
            bounds[1] = (bounds[1] - scaleLow) * overScale;
            var stepError = MaxError;
            var lowStart = bounds[0] - 2f * SearchStep > 0f ? bounds[0] - 2f * SearchStep : 0f;
            var highStart = bounds[1] + 2f * SearchStep < 1f ? bounds[1] + 2f * SearchStep : 1f;
            var low = lowStart;
            var high = highStart;
            for (var l = 0; l < 8; l++, low += SearchStep)
            {
                for (var h = 0; h < 8; h++, high -= SearchStep)
                {
                    var error = RampSearch(stepError, low, high, colours, points);
                    if (error < stepError)
                    {
                        stepError = error;
                        position[0] = low;
                        position[1] = high;
                    }
                }
            }
            position[0] = position[0] * (scaleHigh - scaleLow) + scaleLow;
            position[1] = position[1] * (scaleHigh - scaleLow) + scaleLow;
            if (!((double)stepError + 0.001 < best)) break;
            best = stepError;
            direction.CopyTo(bestDirection);
            position.CopyTo(bestPosition);
            var step = (position[1] - position[0]) / (points - 1);
            var halfStep = step * 0.5f;
            var inverseStep = 1f / step;
            var overType = 1f / (points - 1);
            var average = (points - 1) / 2f;
            for (var i = 0; i < colours; i++)
            {
                var delta = projected0[i] - position[0];
                if (delta <= 0) rampIndices[i] = 0f;
                else if (projected0[i] - position[1] >= 0) rampIndices[i] = points - 1;
                else rampIndices[i] = MathF.Floor((delta + halfStep) * inverseStep);
                rampIndices[i] = (rampIndices[i] - average) * overType;
            }
            cross.Clear();
            var length = 0f;
            for (var i = 0; i < colours; i++)
            {
                var premultiplied = rampIndices[i] * repeats[i];
                length += rampIndices[i] * premultiplied;
                for (var c = 0; c < 3; c++) cross[c] += shifted[i, c] * premultiplied;
            }
            direction.Clear();
            if (length > 0f)
            {
                direction[0] = cross[0] / length;
                direction[1] = cross[1] / length;
                direction[2] = cross[2] / length;
                var norm = MathF.Sqrt(direction[0] * direction[0] + direction[1] * direction[1] + direction[2] * direction[2]);
                direction[0] /= norm;
                direction[1] /= norm;
                direction[2] /= norm;
            }
        }
        for (var e = 0; e < 2; e++)
            for (var c = 0; c < 3; c++) raw[c, e] = (bestPosition[e] * bestDirection[c] + mean[c]) * 255f;
        return true;
    }

    private float RampSearch(float limit, float low, float high, int colours, int points)
    {
        var error = 0f;
        var step = (high - low) / (points - 1);
        var halfStep = step * 0.5f;
        var inverseStep = 1f / step;
        for (var i = 0; i < colours; i++)
        {
            float value;
            var delta = projected[i] - low;
            if (delta <= 0) value = low;
            else if (projected[i] - high >= 0) value = high;
            else value = MathF.Floor((delta + halfStep) * inverseStep) * step + low;
            var distance = projected[i] - value;
            distance *= distance;
            error += weighted[i] * distance + projectionError[i];
            if (limit < error) return limit;
        }
        return error;
    }

    /// <summary>Finds the principal axis, returning true when the block is too small to need one.</summary>
    private bool FindAxis(Span<float> direction, Span<float> centre, int colours)
    {
        Span<float> squares = stackalloc float[3];
        Span<float> correlation = stackalloc float[3];
        direction.Clear();
        centre.Clear();
        var count = 0f;
        for (var i = 0; i < colours; i++)
        {
            centre[0] += unit[i, 0] * repeats[i];
            centre[1] += unit[i, 1] * repeats[i];
            centre[2] += unit[i, 2] * repeats[i];
            count += repeats[i];
        }
        centre[0] /= count;
        centre[1] /= count;
        centre[2] /= count;
        for (var i = 0; i < colours; i++)
        {
            shifted[i, 0] = unit[i, 0] - centre[0];
            shifted[i, 1] = unit[i, 1] - centre[1];
            shifted[i, 2] = unit[i, 2] - centre[2];
            for (var j = 0; j < 3; j++)
            {
                squares[j] += shifted[i, j] * shifted[i, j] * repeats[i];
                correlation[j] += shifted[i, j] * shifted[i, (j + 1) % 3] * repeats[i];
            }
        }
        const float Two = 2f / 255f;
        int i0 = 0, kept = 0;
        var largest = 0f;
        var epsilon = count * Two * Two;
        for (var j = 0; j < 3; j++)
        {
            if (squares[j] >= epsilon) kept++;
            else squares[j] = 0f;
            if (largest < squares[j])
            {
                largest = squares[j];
                i0 = j;
            }
        }
        var epsilon2 = count * 3f * Two * Two;
        if (squares[0] < epsilon2 && squares[1] < epsilon2 && squares[2] < epsilon2) return true;
        if (kept == 1) direction[i0] = 1f;
        else if (kept == 2)
        {
            var i1 = squares[(i0 + 1) % 3] > 0f ? (i0 + 1) % 3 : (i0 + 2) % 3;
            var cross = i1 == (i0 + 1) % 3 ? correlation[i0] : correlation[(i0 + 2) % 3];
            direction[i1] = cross / squares[i0];
            direction[i0] = 1f;
        }
        else
        {
            var determinantLimit = 100000f;
            for (var j = 0; j < 3; j++)
            {
                var determinant = squares[j] * squares[(j + 1) % 3] - correlation[j] * correlation[j];
                if (determinantLimit < determinant)
                {
                    determinantLimit = determinant;
                    i0 = j;
                }
            }
            var v0 = correlation[(i0 + 2) % 3];
            var v1 = correlation[(i0 + 1) % 3];
            var m00 = squares[(i0 + 1) % 3];
            var m11 = squares[i0];
            var m01 = -correlation[i0];
            var r0 = m00 * v0 + m01 * v1;
            var r1 = m01 * v0 + m11 * v1;
            r0 /= determinantLimit;
            r1 /= determinantLimit;
            direction[i0] = 1f;
            direction[(i0 + 1) % 3] = 1f;
            direction[(i0 + 2) % 3] = r0 + r1;
        }
        var length = MathF.Sqrt(direction[0] * direction[0] + direction[1] * direction[1] + direction[2] * direction[2]);
        for (var j = 0; j < 3; j++) direction[j] = length > 0f ? direction[j] / length : 0f;
        return false;
    }

    private void OntoGrid()
    {
        for (var j = 0; j < 3; j++)
            for (var k = 0; k < 2; k++)
            {
                var value = MathF.Floor(raw[j, k]);
                if (value <= 0f) value = 0f;
                else
                {
                    value += MathF.Floor(128f / GridFactors[j]) - MathF.Floor(value / GridFactors[j]);
                    value = 255f < value ? 255f : value;
                }
                onGrid[j, k] = MathF.Floor(value / CellFactors[j]) * CellFactors[j];
            }
    }

    private bool MakeWorking(float[,] input)
    {
        var equal = true;
        for (var j = 0; j < 3; j++) equal &= input[j, 0] == input[j, 1];
        for (var j = 0; j < 3; j++)
            for (var k = 0; k < 2; k++)
            {
                var value = input[j, k] + MathF.Floor(input[j, k] / GridFactors[j]);
                value = value < 0f ? 0f : value;
                working[j, k] = 255f < value ? 255f : value;
            }
        return equal;
    }

    private void BuildChannel(int channel, int points)
    {
        var first = working[channel, 0];
        var last = working[channel, 1];
        ramp[channel, 0] = first;
        ramp[channel, points - 1] = last;
        if (points % 2 != 0) ramp[channel, points] = 1000000f;
        for (var e = 1; e < points - 1; e++)
            ramp[channel, e] = MathF.Floor((first * (points - 1 - e) + last * e + RoundAmount[points]) / (points - 1));
    }

    private void BuildRamp(int points)
    {
        for (var j = 0; j < 3; j++) BuildChannel(j, points);
    }

    /// <summary>Tries each endpoint one grid cell either way, one channel at a time.</summary>
    private void Refine(int colours, int points)
    {
        for (var k = 0; k < 2; k++)
            for (var j = 0; j < 3; j++) original[j, k] = current[j, k] = result[j, k] = onGrid[j, k];
        var equal = MakeWorking(current);
        BuildRamp(points);
        var bestError = RampError(colours, equal ? 1 : points);
        if (bestError == 0f) return;
        bestError = RefineChannel(Red, Green, Blue, colours, points, bestError);
        equal = MakeWorking(current);
        BuildRamp(points);
        bestError = RefineChannel(Green, Red, Blue, colours, points, bestError);
        equal = MakeWorking(current);
        BuildRamp(points);
        RefineChannel(Blue, Red, Green, colours, points, bestError);
        for (var j = 0; j < 3; j++)
            for (var k = 0; k < 2; k++) result[j, k] = current[j, k];
    }

    private float RefineChannel(int channel, int otherA, int otherB, int colours, int points, float bestError)
    {
        for (var i = 0; i < colours; i++)
            for (var r = 0; r < points; r++)
            {
                var distanceA = ramp[otherA, r] - source[i, otherA];
                var distanceB = ramp[otherB, r] - source[i, otherB];
                rampError[r, i] = distanceA * distanceA + distanceB * distanceB;
            }
        var best0 = original[channel, 0];
        var best1 = original[channel, 1];
        var cell = CellFactors[channel];
        for (var i = -1; i <= 1; i++)
            for (var j = -1; j <= 1; j++)
            {
                current[channel, 0] = Limit(original[channel, 0] + i * cell);
                current[channel, 1] = Limit(original[channel, 1] + j * cell);
                var equal = MakeWorking(current);
                BuildChannel(channel, points);
                var mse = 0f;
                var length = equal ? 1 : points;
                for (var k = 0; k < colours; k++)
                {
                    var smallest = 10000000f;
                    for (var r = 0; r < length; r++)
                    {
                        var distance = ramp[channel, r] - source[k, channel];
                        var error = rampError[r, k] + distance * distance;
                        smallest = error < smallest ? error : smallest;
                    }
                    mse += smallest * repeats[k];
                }
                if (mse < bestError)
                {
                    best0 = current[channel, 0];
                    best1 = current[channel, 1];
                    bestError = mse;
                }
            }
        current[channel, 0] = best0;
        current[channel, 1] = best1;
        return bestError;
    }

    private static float Limit(float value)
    {
        value = value < 0f ? 0f : value;
        return 255f < value ? 255f : value;
    }

    private float RampError(int colours, int length)
    {
        var total = 0f;
        for (var i = 0; i < colours; i++)
        {
            var shortest = 99999999999f;
            for (var r = 0; r < length; r++)
            {
                var distance = (source[i, Red] - ramp[Red, r]) * (source[i, Red] - ramp[Red, r])
                    + (source[i, Green] - ramp[Green, r]) * (source[i, Green] - ramp[Green, r])
                    + (source[i, Blue] - ramp[Blue, r]) * (source[i, Blue] - ramp[Blue, r]);
                if (distance < shortest) shortest = distance;
            }
            total += shortest * repeats[i];
        }
        return total;
    }

    /// <summary>Assigns indices from the decoder's point of view, swapping the ramp to match the stored endpoint order.</summary>
    private float Cluster(byte[,] endpoints, byte[] indices, int points, bool useAlpha, uint threshold)
    {
        var c0 = Colour(endpoints, 0);
        var c1 = Colour(endpoints, 1);
        int first = 0, second = 1;
        if (((points & 1) == 0 && c0 <= c1) || ((points & 1) != 0 && c0 > c1))
        {
            first = 1;
            second = 0;
        }
        for (var c = 0; c < 3; c++)
        {
            current[c, 0] = endpoints[c, first];
            current[c, 1] = endpoints[c, second];
        }
        for (var i = 0; i < Pixels; i++)
        {
            pixelColours[i, Red] = (block[i] >> 16) & 0xFF;
            pixelColours[i, Green] = (block[i] >> 8) & 0xFF;
            pixelColours[i, Blue] = block[i] & 0xFF;
            opaque[i] = (block[i] & 0xFF000000) >= threshold;
        }
        var equal = MakeWorking(current);
        BuildRamp(points);
        var length = equal ? 1 : points;
        var total = 0f;
        for (var i = 0; i < Pixels; i++)
        {
            if (useAlpha && !opaque[i])
            {
                indices[i] = (byte)points;
                continue;
            }
            var shortest = 99999999999f;
            var index = 0;
            for (var r = 0; r < length; r++)
            {
                var distance = (pixelColours[i, Red] - ramp[Red, r]) * (pixelColours[i, Red] - ramp[Red, r])
                    + (pixelColours[i, Green] - ramp[Green, r]) * (pixelColours[i, Green] - ramp[Green, r])
                    + (pixelColours[i, Blue] - ramp[Blue, r]) * (pixelColours[i, Blue] - ramp[Blue, r]);
                if (distance < shortest)
                {
                    shortest = distance;
                    index = r;
                }
            }
            total += shortest;
            if (index == points - 1) index = 1;
            else if (index != 0) index++;
            indices[i] = (byte)index;
        }
        return total;
    }

    /// <summary>The cheap axis-projection encoder, used only when a three-colour fit has no usable axis.</summary>
    /// <returns>The block's error, with the block itself left in the fallback words.</returns>
    private float FastBlock()
    {
        Span<float> minimum = [1f, 1f, 1f];
        Span<float> maximum = [0f, 0f, 0f];
        Span<float> average = [0f, 0f, 0f];
        for (var i = 0; i < Pixels; i++)
        {
            var r = ((block[i] >> 16) & 0xFF) / 255f;
            var g = ((block[i] >> 8) & 0xFF) / 255f;
            var b = (block[i] & 0xFF) / 255f;
            pixelColours[i, 0] = r;
            pixelColours[i, 1] = g;
            pixelColours[i, 2] = b;
            minimum[0] = MathF.Min(minimum[0], r);
            minimum[1] = MathF.Min(minimum[1], g);
            minimum[2] = MathF.Min(minimum[2], b);
            maximum[0] = MathF.Max(maximum[0], r);
            maximum[1] = MathF.Max(maximum[1], g);
            maximum[2] = MathF.Max(maximum[2], b);
            saturated[i, 0] = Math.Clamp(r, 0f, 1f);
            saturated[i, 1] = Math.Clamp(g, 0f, 1f);
            saturated[i, 2] = (Math.Clamp(g, 0f, 1f) + Math.Clamp(b, 0f, 1f)) * 0.5f;
            average[0] += saturated[i, 0];
            average[1] += saturated[i, 1];
            average[2] += saturated[i, 2];
        }
        var cLow = Quantize565(minimum, MathF.Floor);
        var cHigh = Quantize565(maximum, MathF.Ceiling);
        if (cLow < cHigh)
            for (var c = 0; c < 3; c++) average[c] *= 0.0625f;
        Span<float> axis = [0f, 0f, 0f];
        float redGreen = 0f, blueGreen = 0f, redBlue = 0f;
        for (var i = 0; i < Pixels; i++)
        {
            var x = saturated[i, 0] - average[0];
            var y = saturated[i, 1] - average[1];
            var z = saturated[i, 2] - average[2];
            axis[0] += MathF.Abs(x);
            axis[1] += MathF.Abs(y);
            axis[2] += MathF.Abs(z);
            if (x > 0f)
            {
                redGreen += y;
                redBlue += z;
            }
            if (z > 0f) blueGreen += y;
        }
        for (var c = 0; c < 3; c++) axis[c] *= 0.0625f;
        if (redGreen < 0f) axis[0] = -axis[0];
        if (blueGreen < 0f) axis[2] = -axis[2];
        if (redGreen == blueGreen && redGreen == 0f && redBlue < 0f) axis[2] = -axis[2];
        var squared = axis[0] * axis[0] + axis[1] * axis[1] + axis[2] * axis[2];
        var scale = squared > 0f ? 1f / MathF.Sqrt(squared) : 1f;
        for (var c = 0; c < 3; c++) axis[c] *= scale;
        var left = float.MaxValue;
        var right = -float.MaxValue;
        for (var i = 0; i < Pixels; i++)
        {
            var position = (saturated[i, 0] - average[0]) * axis[0] + (saturated[i, 1] - average[1]) * axis[1] + (saturated[i, 2] - average[2]) * axis[2];
            axisPositions[i] = position;
            if (position < left) left = position;
            if (position > right) right = position;
        }
        var centre = (left + right) * 0.5f;
        for (var c = 0; c < 3; c++) average[c] += axis[c] * centre;
        for (var i = 0; i < Pixels; i++) axisPositions[i] -= centre;
        right -= centre;
        left -= centre;
        Span<float> lowColour = [average[0] + axis[0] * left, average[1] + axis[1] * left, average[2] + axis[2] * left];
        Span<float> highColour = [average[0] + axis[0] * right, average[1] + axis[1] * right, average[2] + axis[2] * right];
        lowColour[2] = lowColour[2] * 2f - lowColour[1];
        highColour[2] = highColour[2] * 2f - highColour[1];
        var c0 = Quantize565(lowColour, Round);
        var c1 = Quantize565(highColour, Round);
        var swap = 0u;
        if (c0 < c1)
        {
            (c0, c1) = (c1, c0);
            swap = 1;
        }
        else if (c0 == c1) Array.Fill(axisPositions, left);
        var word0 = c0 | c1 << 16;
        var word1 = 0u;
        var division = right * 2f / 3f;
        centre = (left + right) / 2;
        for (var i = 0; i < Pixels; i++)
        {
            var index = MathF.Abs(axisPositions[i]) >= division ? 0u : 2u;
            if (axisPositions[i] >= centre) index += 1;
            word1 |= (index ^ swap) << (2 * i);
        }
        var error = BlockError(word0, word1);
        var blank = BlockError(0, 0);
        if (error > blank)
        {
            word0 = 0;
            word1 = 0;
            error = blank;
        }
        fallback0 = word0;
        fallback1 = word1;
        return error;
    }

    private static float Round(float value) => MathF.Round(value, MidpointRounding.AwayFromZero);

    private static uint Quantize565(ReadOnlySpan<float> colour, Func<float, float> round)
    {
        var r = (uint)round(Math.Clamp(colour[0], 0f, 1f) * 31f);
        var g = (uint)round(Math.Clamp(colour[1], 0f, 1f) * 63f);
        var b = (uint)round(Math.Clamp(colour[2], 0f, 1f) * 31f);
        return r << 11 | g << 5 | b;
    }

    private static (float R, float G, float B) Expand(uint colour)
    {
        var r = (colour & 0xF800) >> 8;
        var g = (colour & 0x07E0) >> 3;
        var b = (colour & 0x001F) << 3;
        return (r + (r >> 5), g + (g >> 6), b + (b >> 5));
    }

    private float BlockError(uint word0, uint word1)
    {
        var low = word0 & 0xFFFF;
        var high = word0 >> 16;
        var c0 = Expand(low);
        var c1 = Expand(high);
        var four = low > high;
        var c2 = four ? ((c0.R * 2f + c1.R) / 3f, (c0.G * 2f + c1.G) / 3f, (c0.B * 2f + c1.B) / 3f) : ((c0.R + c1.R) / 2f, (c0.G + c1.G) / 2f, (c0.B + c1.B) / 2f);
        var c3 = four ? ((c1.R * 2f + c0.R) / 3f, (c1.G * 2f + c0.G) / 3f, (c1.B * 2f + c0.B) / 3f) : (0f, 0f, 0f);
        float red = 0f, green = 0f, blue = 0f;
        for (var i = 0; i < Pixels; i++)
        {
            var (r, g, b) = ((word1 >> (2 * i)) & 3) switch { 0 => c0, 1 => c1, 2 => c2, _ => c3 };
            var dr = Round(pixelColours[i, 0] * 255f) - r;
            var dg = Round(pixelColours[i, 1] * 255f) - g;
            var db = Round(pixelColours[i, 2] * 255f) - b;
            red += dr * dr;
            green += dg * dg;
            blue += db * db;
        }
        return (red + green + blue) / 48f;
    }

    private void Alpha(Span<byte> target)
    {
        for (var i = 0; i < Pixels; i++) alphaValues[i] = alphaBytes[i] / 255f;
        var error8 = AlphaFit(8, false, alphaIndices8, out var low8, out var high8);
        byte low6 = 0, high6 = 0;
        var error6 = error8 == 0f ? float.MaxValue : AlphaFit(6, true, alphaIndices6, out low6, out high6);
        var eight = error8 <= error6;
        var indices = eight ? alphaIndices8 : alphaIndices6;
        target[0] = eight ? low8 : low6;
        target[1] = eight ? high8 : high6;
        var bits = 0UL;
        for (var i = 0; i < Pixels; i++) bits |= (ulong)(indices[i] & 7) << (3 * i);
        for (var i = 0; i < 6; i++) target[2 + i] = (byte)(bits >> (8 * i));
    }

    private float AlphaFit(int points, bool fixedPoints, byte[] indices, out byte first, out byte second)
    {
        Span<float> endpoints = stackalloc float[2];
        AlphaEndpoints(endpoints, points, fixedPoints);
        var error = AlphaCluster(indices, endpoints, points, fixedPoints);
        first = (byte)(int)endpoints[0];
        second = (byte)(int)endpoints[1];
        return error;
    }

    private void AlphaEndpoints(Span<float> output, int points, bool fixedPoints)
    {
        const float Scale = 255f;
        Array.Copy(alphaValues, alphaSorted, Pixels);
        Array.Sort(alphaSorted);
        Array.Clear(alphaUnique);
        Array.Clear(alphaRepeats);
        var previous = -2f;
        var count = 0;
        var calculate = true;
        float first, second;
        if (fixedPoints)
        {
            for (var i = 0; i < Pixels; i++)
            {
                if (previous != alphaSorted[i])
                {
                    previous = alphaSorted[i];
                    if ((double)previous <= 1.5 / 255.0) continue;
                    if ((double)previous >= 253.5 / 255.0) continue;
                    alphaUnique[count] = alphaSorted[i];
                    alphaRepeats[count] = 1f;
                    count++;
                }
                else if (count > 0 && alphaUnique[count - 1] == previous) alphaRepeats[count - 1] += 1f;
            }
            first = second = 0f;
            if (count <= 2)
            {
                if (count == 2)
                {
                    first = MathF.Floor(alphaUnique[0] * Scale + 0.5f);
                    second = MathF.Floor(alphaUnique[1] * Scale + 0.5f);
                }
                else if (count == 1)
                {
                    first = MathF.Floor(alphaUnique[0] * Scale + 0.5f);
                    second = first + 1f;
                }
                else
                {
                    first = 128f;
                    second = first + 1f;
                }
                calculate = false;
            }
        }
        else
        {
            for (var i = 0; i < Pixels; i++)
            {
                if (previous != alphaSorted[i])
                {
                    alphaUnique[count] = previous = alphaSorted[i];
                    alphaRepeats[count] = 1f;
                    count++;
                }
                else alphaRepeats[count - 1] += 1f;
            }
            first = second = 0f;
            if (count <= 2)
            {
                first = MathF.Floor(alphaUnique[0] * Scale + 0.5f);
                second = count == 1 ? first + 1f : MathF.Floor(alphaUnique[1] * Scale + 0.5f);
                calculate = false;
            }
        }
        if (calculate)
        {
            var minimum = alphaUnique[0];
            var maximum = alphaUnique[count - 1];
            var left = minimum;
            var right = maximum;
            float bestLeft = 0f, bestRight = 0f;
            var centre = (left + right) / 2;
            var bestError = MaxError;
            if (!(maximum - minimum <= 48f / 256f))
            {
                var leftBound = 0f > left - GlobalExtent ? 0f : left - GlobalExtent;
                var rightBound = 1f < right + GlobalExtent ? 1f : right + GlobalExtent;
                var leftLimit = centre < left + GlobalExtent ? centre : left + GlobalExtent;
                var rightLimit = centre > right - GlobalExtent ? centre : right - GlobalExtent;
                for (var stepLeft = leftBound; stepLeft < leftLimit; stepLeft += GlobalStep)
                    for (var stepRight = rightBound; rightLimit <= stepRight; stepRight -= GlobalStep)
                    {
                        var error = AlphaSearch(bestError, stepLeft, stepRight, count, points);
                        if (error < bestError)
                        {
                            bestError = error;
                            bestLeft = stepLeft;
                            bestRight = stepRight;
                        }
                    }
                left = bestLeft;
                right = bestRight;
            }
            AlphaRefine(bestError, ref left, ref right, LocalStep / 256f, 0f, 1f, count, points);
            second = MathF.Floor(right * Scale + 0.5f);
            first = MathF.Floor(left * Scale + 0.5f);
        }
        if (first == second)
        {
            if (second < 255f) second += 1f;
            else second -= 1f;
        }
        output[0] = first;
        output[1] = second;
    }

    private float AlphaSearch(float limit, float low, float high, int count, int points)
    {
        var error = 0f;
        var step = (high - low) / (points - 1);
        var halfStep = step * 0.5f;
        var inverseStep = 1f / step;
        for (var i = 0; i < count; i++)
        {
            float value;
            var delta = alphaUnique[i] - low;
            if (delta <= 0) value = low;
            else if (alphaUnique[i] - high >= 0) value = high;
            else value = MathF.Floor((delta + halfStep) * inverseStep) * step + low;
            var distance = alphaUnique[i] - value;
            error += distance * distance * alphaRepeats[i];
            if (limit < error) return limit;
        }
        return error;
    }

    private void AlphaRefine(float limit, ref float low, ref float high, float step, float lowBound, float highBound, int count, int points)
    {
        var best = limit;
        var left = low;
        var right = high;
        int bestMode;
        do
        {
            var candidateLeft = left;
            var candidateRight = right;
            bestMode = -1;
            for (var mode = 0; mode < MoveSteps * MoveSteps; mode++)
            {
                var trialLeft = left + step * Moves[mode / MoveSteps];
                var trialRight = right + step * Moves[mode % MoveSteps];
                trialLeft = trialLeft < lowBound ? lowBound : trialLeft;
                trialRight = highBound < trialRight ? highBound : trialRight;
                var error = AlphaSearch(best, trialLeft, trialRight, count, points);
                if (error < best)
                {
                    best = error;
                    bestMode = mode;
                    candidateLeft = trialLeft;
                    candidateRight = trialRight;
                }
            }
            if (bestMode != -1)
            {
                left = candidateLeft;
                right = candidateRight;
            }
        }
        while (bestMode != -1);
        low = left;
        high = right;
    }

    private float AlphaCluster(byte[] indices, Span<float> endpoints, int points, bool fixedPoints)
    {
        Array.Clear(indices);
        if (endpoints[0] == endpoints[1]) return 0f;
        if ((!fixedPoints && endpoints[0] <= endpoints[1]) || (fixedPoints && endpoints[0] > endpoints[1]))
            (endpoints[0], endpoints[1]) = (endpoints[1], endpoints[0]);
        for (var e = points; e < 16; e++) alphaRamp[e] = 100000f;
        alphaRamp[0] = endpoints[0];
        alphaRamp[1] = endpoints[1];
        for (var e = 1; e < points - 1; e++)
            alphaRamp[e + 1] = (alphaRamp[0] * (points - 1 - e) + alphaRamp[1] * e) / (points - 1);
        if (fixedPoints)
        {
            alphaRamp[points] = 0f;
            alphaRamp[points + 1] = 255f;
        }
        for (var i = 0; i < points; i++) alphaRamp[i] = MathF.Floor(alphaRamp[i] + 0.5f);
        var total = fixedPoints ? points + 2 : points;
        var overRange = 1f / 255f;
        for (var i = 0; i < total; i++) alphaRamp[i] *= overRange;
        var sum = 0f;
        for (var i = 0; i < Pixels; i++)
        {
            var shortest = 10000000f;
            var value = alphaValues[i];
            for (var j = 0; j < total; j++)
            {
                var distance = value - alphaRamp[j];
                distance *= distance;
                if (distance < shortest)
                {
                    shortest = distance;
                    indices[i] = (byte)j;
                }
            }
            sum += shortest;
        }
        return sum;
    }
}
